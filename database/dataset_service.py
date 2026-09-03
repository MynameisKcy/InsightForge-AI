"""数据集生命周期事务属主（架构评审 R2 候选6）。

上传/删除/schema 探测此前内联在 api/routes/datasets.py 的 ~110 行事务脚本里
（校验→落盘→DuckDB 装载→schema/sample 探测→元数据→失败补偿），且 upload 与
schema 两条路径各自维护一份 sample 序列化（f23b010 只修了 upload 侧）。
本模块收口为一个深模块：

- 事务语义（失败补偿：装载/探测/元数据任一步失败即清理已落盘文件）只此一份；
- sample 序列化统一走 to_json（Timestamp→ISO、NaN→null），两条路径同参；
- 构造期注入（duckdb_factory/meta_provider/dir_provider）供测试隔离，
  缺省懒解析生产单例（database.duckdb_manager.init_duckdb /
  database.datasources_db.datasources_db），与路由期函数级 import 的
  monkeypatch 接缝兼容（见 tests/test_datasets_api.py）。

错误契约：DatasetServiceError(message, status_code)；路由层只做异常→HTTP 映射。
"""
import json
import os
import re as re_module
import traceback

from database.safety import safe_ident
from utils.logger_handler import logger
from utils.path_tool import get_abs_path

ALLOWED_DATASET_TYPES = {"csv", "xlsx", "xls"}
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB（知识库上传共用，见 api/routes/knowledge.py）

# 规则简述：字段列上限（超出截断为 "…等 N 个字段"），避免超长脏数据刷屏
_DESC_MAX_FIELDS = 8


def _build_description(display_name: str, row_count: int, columns: list) -> str:
    """规则生成数据集内容简述（无 LLM 依赖）。

    供 DataResolver 的 n-gram 匹配（description 是匹配源之一）与歧义候选列表
    展示（帮用户分辨查哪个数据集）。字段名列在 display_name 之后，让
    「用户输入内容相关词」也能命中——如列名 city/人口 出现在 query 时。
    """
    names = [c[0] for c in columns] if columns else []
    fields = "、".join(names[:_DESC_MAX_FIELDS])
    if len(names) > _DESC_MAX_FIELDS:
        fields += f"…等{len(names)}个字段"
    return f"{display_name} 数据，共{row_count}行，含字段：{fields}"


class DatasetServiceError(Exception):
    """数据集操作失败；status_code 由路由映射为 HTTP 状态。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _default_datasets_dir() -> str:
    """用户上传的数据集存放目录（生产缺省）。"""
    d = get_abs_path("data/datasets")
    os.makedirs(d, exist_ok=True)
    return d


class DatasetService:
    """数据集上传/删除/schema 的事务编排（文件 + DuckDB 表 + 元数据三者一致）。"""

    ALLOWED_TYPES = ALLOWED_DATASET_TYPES
    MAX_SIZE = MAX_UPLOAD_SIZE

    def __init__(self, duckdb_factory=None, meta_provider=None, dir_provider=None):
        # 注入优先：测试传隔离桩；缺省懒解析生产单例（调用期 import，
        # 保持对 database.duckdb_manager / database.datasources_db 的 monkeypatch 接缝）
        self._duckdb_factory = duckdb_factory
        self._meta_provider = meta_provider
        self._dir_provider = dir_provider

    # ── 接缝解析 ──

    def _duck(self, user_id: str):
        if self._duckdb_factory is not None:
            return self._duckdb_factory(user_id)
        from database.duckdb_manager import init_duckdb
        return init_duckdb(user_id=user_id)

    def _meta(self):
        if self._meta_provider is not None:
            return self._meta_provider()
        from database.datasources_db import datasources_db
        return datasources_db

    def _dir(self) -> str:
        if self._dir_provider is not None:
            return self._dir_provider()
        return _default_datasets_dir()

    # ── 事务 ──

    def upload(self, content: bytes, filename: str, user_id: str) -> dict:
        """上传事务：校验→落盘→DuckDB 装载→探测→元数据；任一步失败清理文件并抛错。"""
        fname = os.path.basename(filename or "")
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        if ext not in self.ALLOWED_TYPES:
            raise DatasetServiceError(
                f"不支持的文件类型: {ext}，仅支持 CSV/XLSX/XLS", status_code=400)
        if len(content) > self.MAX_SIZE:
            raise DatasetServiceError("文件超过大小限制(100MB)", status_code=413)

        table_name = self._resolve_table_name(fname, ext, user_id)
        fpath = os.path.join(self._dir(), f"{table_name}.{ext}")
        with open(fpath, "wb") as out:
            out.write(content)

        try:
            db = self._duck(user_id)
            if ext == "csv":
                load_result = db.load_csv_dataset(fpath, table_name)
            else:
                load_result = db.load_excel_dataset(fpath, table_name)
            if not load_result["success"]:
                os.remove(fpath)
                raise DatasetServiceError(load_result["error"], status_code=400)

            columns, sample = self._probe(db, table_name)

            # display_name: 原始文件名去扩展名，保留中文，供侧边栏展示与 DataResolver 匹配
            # （table_name 是安全化 ASCII 名，用户无法对应；display_name 是用户能认得的名字）
            display_name = os.path.splitext(fname)[0].strip()
            source_type = "csv" if ext == "csv" else "excel"
            schema_json = json.dumps(
                [{"name": c[0], "type": c[1]} for c in columns], ensure_ascii=False)
            # 规则简述：入库即存（C3，供 DataResolver 匹配与候选展示）
            description = _build_description(
                display_name, load_result["row_count"], columns)

            meta_result = self._meta().add_dataset(
                name=table_name,
                source_type=source_type,
                file_path=fpath,
                table_name=table_name,
                schema_json=schema_json,
                row_count=load_result["row_count"],
                description=description,
                owner_user_id=user_id,
                display_name=display_name,
            )
            # 元数据写入失败（如 UNIQUE 冲突）：删除文件并明确报错，
            # 避免"上传报成功但侧边栏查不到"
            if not meta_result.get("success"):
                self._remove_file_quietly(fpath)
                raise DatasetServiceError(
                    f"元数据写入失败: {meta_result.get('error', '未知错误')}", status_code=400)

            return {
                "success": True,
                "name": table_name,
                "display_name": display_name,
                "source_type": source_type,
                "row_count": load_result["row_count"],
                "columns": [c[0] for c in columns],
                "sample": sample,
            }
        except DatasetServiceError:
            raise
        except Exception as e:
            logger.error(f"Dataset upload failed: {traceback.format_exc()}")
            self._remove_file_quietly(fpath)
            raise DatasetServiceError(str(e), status_code=500) from e

    def delete(self, name: str, user_id: str) -> None:
        """删除数据集（卸载 DuckDB 表 + 删除文件 + 删除元数据）。"""
        ds = self._meta().get_dataset(name, owner_user_id=user_id)
        if not ds:
            raise DatasetServiceError(
                f"数据集 '{name}' 不存在或不属于当前用户", status_code=404)

        try:
            self._duck(user_id).drop_table(ds["table_name"])
        except Exception as e:
            logger.warning(f"Failed to drop table {ds['table_name']}: {e}")

        # 删除文件（路径穿越防护：仅允许删除 datasets 目录下的文件）
        if ds["file_path"] and os.path.exists(ds["file_path"]):
            try:
                allowed_dir = os.path.abspath(self._dir())
                real_path = os.path.realpath(ds["file_path"])
                if real_path.startswith(allowed_dir + os.sep):
                    os.remove(real_path)
                else:
                    logger.warning(f"Refusing to delete file outside datasets dir: {real_path}")
            except Exception as e:
                logger.warning(f"Failed to delete file {ds['file_path']}: {e}")

        self._meta().delete_dataset(name, owner_user_id=user_id)

    def schema(self, name: str, user_id: str) -> dict:
        """数据集 schema：DuckDB 实时探测（列/统计/样本），失败回退元数据缓存。"""
        ds = self._meta().get_dataset(name, owner_user_id=user_id)
        if not ds:
            raise DatasetServiceError(
                f"数据集 '{name}' 不存在或不属于当前用户", status_code=404)

        try:
            db = self._duck(user_id)
            qname = safe_ident(ds["table_name"])
            columns, sample = self._probe(db, ds["table_name"])
            stats = db.execute_fetchall(f"SUMMARIZE {qname}")
            return {
                "name": name,
                "table_name": ds["table_name"],
                "source_type": ds["source_type"],
                "row_count": ds["row_count"],
                "columns": [{"name": c[0], "type": c[1]} for c in columns],
                "statistics": [
                    {"column": s[0], "type": s[1], "min": str(s[2]) if s[2] is not None else None,
                     "max": str(s[3]) if s[3] is not None else None,
                     "avg": str(s[4]) if s[4] is not None else None,
                     "std": str(s[5]) if s[5] is not None else None,
                     "count": s[6], "null_count": s[7]}
                    for s in stats
                ],
                "sample": sample,
            }
        except Exception:
            # DuckDB 中表可能尚未加载，返回元数据中的 schema_json
            return {
                "name": name,
                "table_name": ds["table_name"],
                "source_type": ds["source_type"],
                "row_count": ds["row_count"],
                "columns": json.loads(ds.get("schema_json", "[]")),
                "note": "DuckDB 中未加载，显示的是缓存 schema",
            }

    # ── 内部 ──

    def _resolve_table_name(self, fname: str, ext: str, user_id: str) -> str:
        """生成安全表名（ASCII 化）并处理同名冲突（追加 _2/_3…）。"""
        base_name = os.path.splitext(fname)[0]
        safe_name = re_module.sub(r'[^A-Za-z0-9]+', '_', base_name).strip('_')
        if not safe_name or not safe_name[0].isalpha():
            safe_name = "ds_" + (safe_name or "upload")

        table_name = safe_name
        counter = 2
        while self._meta().get_dataset(table_name, owner_user_id=user_id):
            table_name = f"{safe_name}_{counter}"
            counter += 1
        return table_name

    def _probe(self, db, table_name: str):
        """探测列与样本（统一序列化：to_json——Timestamp→ISO、NaN→null）。

        upload 与 schema 两条路径共用（f23b010 曾只修 upload 侧，序列化形态分叉）。
        样本探测失败容错为 []（DESCRIBE 失败仍上抛：upload 报 500、schema 回退缓存）。
        """
        qname = safe_ident(table_name)
        cols = db.execute_fetchall(f"DESCRIBE {qname}")
        try:
            sample_df = db.query_df(f"SELECT * FROM {qname} LIMIT 5")
            # to_json(date_format="iso")：日期列 → ISO 字符串、NaN → null。
            # 注意默认 date_format="epoch" 会把 Timestamp 变毫秒数（f23b010 后
            # upload 路径的真实形态，注释却自称 ISO）；schema 路径则走
            # _json_safe 产出 ISO——两路形态分叉，此处统一为 ISO。
            sample = json.loads(sample_df.to_json(
                orient="records", force_ascii=False, date_format="iso"))
        except Exception:
            sample = []
        return cols, sample

    @staticmethod
    def _remove_file_quietly(fpath: str) -> None:
        try:
            if os.path.exists(fpath):
                os.remove(fpath)
        except OSError:
            pass


# ── 模块级单例（路由经此引用；测试也可 patch 本属性整体换桩） ──
dataset_service = DatasetService()
