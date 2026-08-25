"""数据集管理路由：/api/datasets 列表/上传/删除/schema + /api/datasources/reload。

datasources_db / init_duckdb 等保持函数级懒 import（重依赖晚加载，且是
tests monkeypatch 的属主模块接缝，见 test_datasources_reload_reindex_api）。
"""
import json
import os
import re as re_module
import traceback

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse

from api.auth import require_auth
from api.serialization import _json_safe
from utils.logger_handler import logger
from utils.path_tool import get_abs_path

router = APIRouter()

_ALLOWED_DATASET_TYPES = {"csv", "xlsx", "xls"}
_MAX_DATASET_SIZE = 100 * 1024 * 1024  # 100MB


def _datasets_dir() -> str:
    """用户上传的数据集存放目录。"""
    d = get_abs_path("data/datasets")
    os.makedirs(d, exist_ok=True)
    return d


@router.get("/api/datasets")
async def list_datasets(request: Request, user=Depends(require_auth)):
    """列出所有可用数据集。"""
    user_id = user["user_id"]
    from database.datasources_db import datasources_db
    datasets = datasources_db.list_datasets(owner_user_id=user_id)
    return JSONResponse({"datasets": datasets, "count": len(datasets)})


@router.post("/api/datasets/upload")
async def upload_dataset(request: Request, file: UploadFile = File(...), user=Depends(require_auth)):
    """上传 CSV/Excel 文件，解析并加载到 DuckDB。"""
    user_id = user["user_id"]

    fname = os.path.basename(file.filename or "")
    ext = os.path.splitext(fname)[1].lower().lstrip(".")
    if ext not in _ALLOWED_DATASET_TYPES:
        return JSONResponse(
            {"success": False, "error": f"不支持的文件类型: {ext}，仅支持 CSV/XLSX/XLS"},
            status_code=400,
        )

    content = await file.read()
    if len(content) > _MAX_DATASET_SIZE:
        return JSONResponse(
            {"success": False, "error": "文件超过大小限制(100MB)"},
            status_code=413,
        )

    # 保存文件
    ds_dir = _datasets_dir()
    # 生成安全的表名：文件名去扩展名，替换非法字符
    base_name = os.path.splitext(fname)[0]
    safe_name = re_module.sub(r'[^A-Za-z0-9]+', '_', base_name).strip('_')
    if not safe_name or not safe_name[0].isalpha():
        safe_name = "ds_" + (safe_name or "upload")

    # 处理同名冲突
    from database.datasources_db import datasources_db

    table_name = safe_name
    counter = 2
    while datasources_db.get_dataset(table_name, owner_user_id=user_id):
        table_name = f"{safe_name}_{counter}"
        counter += 1

    fpath = os.path.join(ds_dir, f"{table_name}.{ext}")
    with open(fpath, "wb") as out:
        out.write(content)

    # 加载到 DuckDB
    from database.duckdb_manager import init_duckdb
    from database.safety import safe_ident

    try:
        db = init_duckdb(user_id=user_id)
        if ext == "csv":
            load_result = db.load_csv_dataset(fpath, table_name)
        else:
            load_result = db.load_excel_dataset(fpath, table_name)

        if not load_result["success"]:
            # 加载失败，删除文件
            os.remove(fpath)
            return JSONResponse({"success": False, "error": load_result["error"]}, status_code=400)

        # 解析 schema
        qname = safe_ident(table_name)
        cols = db.execute(f"DESCRIBE {qname}").fetchall()
        schema_json = json.dumps([
            {"name": c[0], "type": c[1]} for c in cols
        ], ensure_ascii=False)

        # 获取样本数据（前5行）
        try:
            sample_df = db.query_df(f"SELECT * FROM {qname} LIMIT 5")
            # to_json 把日期列转 ISO 字符串；to_dict 会保留 pd.Timestamp，导致响应 JSON 序列化 500
            sample_data = json.loads(sample_df.to_json(orient="records", force_ascii=False))
        except Exception:
            sample_data = []

        # 写入元数据（带 owner_user_id 实现多用户隔离）
        source_type = "csv" if ext == "csv" else "excel"
        # display_name: 原始文件名去扩展名，保留中文，供侧边栏展示与 DataResolver 匹配
        # （table_name 是安全化 ASCII 名，用户无法对应；display_name 是用户能认得的名字）
        display_name = os.path.splitext(fname)[0].strip()
        meta_result = datasources_db.add_dataset(
            name=table_name,
            source_type=source_type,
            file_path=fpath,
            table_name=table_name,
            schema_json=schema_json,
            row_count=load_result["row_count"],
            owner_user_id=user_id,
            display_name=display_name,
        )
        # 元数据写入失败（如 UNIQUE 冲突）：删除文件并明确报错，避免"上传报成功但侧边栏查不到"
        if not meta_result.get("success"):
            try:
                os.remove(fpath)
            except OSError:
                pass
            return JSONResponse(
                {"success": False, "error": f"元数据写入失败: {meta_result.get('error', '未知错误')}"},
                status_code=400,
            )

        return JSONResponse({
            "success": True,
            "name": table_name,
            "display_name": display_name,
            "source_type": source_type,
            "row_count": load_result["row_count"],
            "columns": [c[0] for c in cols],
            "sample": _json_safe(sample_data),
        })

    except Exception as e:
        logger.error(f"Dataset upload failed: {traceback.format_exc()}")
        if os.path.exists(fpath):
            os.remove(fpath)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.delete("/api/datasets/{name}")
async def delete_dataset(request: Request, name: str, user=Depends(require_auth)):
    """删除数据集（卸载 DuckDB 表 + 删除文件 + 删除元数据）。"""
    user_id = user["user_id"]
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        return JSONResponse({"error": "非法数据集名"}, status_code=400)

    from database.datasources_db import datasources_db

    ds = datasources_db.get_dataset(name, owner_user_id=user_id)
    if not ds:
        return JSONResponse({"error": f"数据集 '{name}' 不存在或不属于当前用户"}, status_code=404)

    # 从 DuckDB 删除表
    from database.duckdb_manager import init_duckdb

    try:
        db = init_duckdb(user_id=user_id)
        db.drop_table(ds["table_name"])
    except Exception as e:
        logger.warning(f"Failed to drop table {ds['table_name']}: {e}")

    # 删除文件（路径穿越防护：仅允许删除 datasets 目录下的文件）
    if ds["file_path"] and os.path.exists(ds["file_path"]):
        try:
            allowed_dir = os.path.abspath(_datasets_dir())
            real_path = os.path.realpath(ds["file_path"])
            if real_path.startswith(allowed_dir + os.sep):
                os.remove(real_path)
            else:
                logger.warning(f"Refusing to delete file outside datasets dir: {real_path}")
        except Exception as e:
            logger.warning(f"Failed to delete file {ds['file_path']}: {e}")

    # 删除元数据（带归属校验，防越权）
    datasources_db.delete_dataset(name, owner_user_id=user_id)
    return JSONResponse({"success": True})


@router.get("/api/datasets/{name}/schema")
async def get_dataset_schema(request: Request, name: str, user=Depends(require_auth)):
    """获取数据集的详细 schema。"""
    user_id = user["user_id"]

    from database.datasources_db import datasources_db

    ds = datasources_db.get_dataset(name, owner_user_id=user_id)
    if not ds:
        return JSONResponse({"error": f"数据集 '{name}' 不存在或不属于当前用户"}, status_code=404)

    # 从 DuckDB 获取实时 schema
    from database.duckdb_manager import init_duckdb
    from database.safety import safe_ident

    try:
        db = init_duckdb(user_id=user_id)
        qname = safe_ident(ds['table_name'])
        cols = db.execute(f"DESCRIBE {qname}").fetchall()
        stats = db.execute(f"SUMMARIZE {qname}").fetchall()
        sample_df = db.query_df(f"SELECT * FROM {qname} LIMIT 5")

        return JSONResponse({
            "name": name,
            "table_name": ds["table_name"],
            "source_type": ds["source_type"],
            "row_count": ds["row_count"],
            "columns": [{"name": c[0], "type": c[1]} for c in cols],
            "statistics": [
                {"column": s[0], "type": s[1], "min": str(s[2]) if s[2] is not None else None,
                 "max": str(s[3]) if s[3] is not None else None,
                 "avg": str(s[4]) if s[4] is not None else None,
                 "std": str(s[5]) if s[5] is not None else None,
                 "count": s[6], "null_count": s[7]}
                for s in stats
            ],
            "sample": _json_safe(sample_df.to_dict(orient="records")),
        })
    except Exception:
        # DuckDB 中表可能尚未加载，返回元数据中的 schema_json
        import json as _json
        return JSONResponse({
            "name": name,
            "table_name": ds["table_name"],
            "source_type": ds["source_type"],
            "row_count": ds["row_count"],
            "columns": _json.loads(ds.get("schema_json", "[]")),
            "note": "DuckDB 中未加载，显示的是缓存 schema",
        })


@router.post("/api/datasources/reload")
async def reload_datasources(request: Request, user=Depends(require_auth)):
    """热加载 datasources.yml 配置的数据库连接。"""
    user_id = user["user_id"]

    from database.duckdb_manager import init_duckdb

    try:
        db = init_duckdb(user_id=user_id)
        result = db.register_external_databases()
        return JSONResponse({"success": True, **result})
    except Exception as e:
        logger.error(f"Datasource reload failed: {traceback.format_exc()}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
