"""
DuckDB Manager: Load CSV data into DuckDB and provide query/execution interface.
"""

import html
import os
import re
import threading
import unicodedata
from collections import OrderedDict

import duckdb
import pandas as pd

from database.customer_profiles import persist_customer_profiles
from database.external_sources import register_external_databases as _register_external
from database.safety import (
    assert_read_only,
    safe_ident,
    validate_csv_path,
    validate_table_name,
)
from utils.logger_handler import logger

# 不可见/干扰字符：零宽、BOM、软连字符、大部分控制字符（保留制表/换行待折叠）
_INVISIBLE_RE = re.compile(r"[​-‏‪-‮⁠﻿]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_WS_RE = re.compile(r"\s+")


def _normalize_one_column_name(name: str, idx: int) -> str:
    """清洗单个列名：HTML 实体解码、NBSP/全角归一、去不可见/控制字符、折叠空白。

    用户上传的 Excel/CSV 表头常含 ``&nbsp;`` 等 HTML 实体、非断行空格(U+00A0)、
    全角字符、零宽字符、BOM 等干扰项，原样进入 DuckDB 列名会导致 SQL 引用困难、
    图表标签乱码。此处统一归一为干净可读的列名。
    """
    s = "" if name is None else str(name)
    s = html.unescape(s)                       # &nbsp; -> \xa0, &amp; -> &, &#160; -> \xa0
    s = unicodedata.normalize("NFKC", s)       # NBSP->空格、全角->半角
    s = _INVISIBLE_RE.sub("", s)               # 去零宽/BOM/方向标记
    s = _CONTROL_RE.sub("", s)                 # 去控制字符
    s = _WS_RE.sub(" ", s).strip()             # 折叠空白
    if not s:
        s = f"column_{idx + 1}"
    return s


def _normalize_column_names(names: list[str]) -> list[str]:
    """批量清洗列名并去重（重名追加 _2/_3），返回与输入等长的新列名列表。"""
    cleaned = [_normalize_one_column_name(n, i) for i, n in enumerate(names)]
    seen: dict[str, int] = {}
    result: list[str] = []
    for n in cleaned:
        if n in seen:
            seen[n] += 1
            result.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 1
            result.append(n)
    return result


def _duckdb_limits() -> dict:
    """读取 config/agent.yml `duckdb` 节的查询通道资源上限（防御式默认）。

    只读 AST 沙箱不拦"合法但昂贵"的查询，资源上限是其 DoS 防护补全：
    memory_limit/threads 经 connect(config=) 生效（SET/PRAGMA 被沙箱双拒，
    不能走 SQL），max_result_rows / query_timeout 在查询通道 Python 侧强制。
    """
    try:
        from utils.config_handler import agent_conf
        conf = (agent_conf or {}).get("duckdb", {}) or {}
    except Exception:
        conf = {}
    return {
        "memory_limit": str(conf.get("memory_limit", "1GB")),
        "threads": int(conf.get("threads", 2)),
        "max_result_rows": int(conf.get("max_result_rows", 10000)),
        "query_timeout_seconds": float(conf.get("query_timeout_seconds", 30)),
    }


class DuckDBManager:
    """Manages a DuckDB in-memory database, loads CSV data, and executes queries.

    不再是进程级单例：每个实例拥有独立的 :memory: 连接，按 user_id 隔离数据，
    避免多用户并发时互相覆盖表数据。通过 init_duckdb(user_id) 工厂按 user_id 缓存实例。
    """

    def __init__(self, csv_path: str | None = None, table_name: str = "transactions", user_id: str = "default"):
        validate_table_name(table_name)
        self.user_id = user_id
        self.table_name = table_name
        self.last_loaded_csv: str | None = None  # 本实例上次加载的 CSV，用于判断是否需要 reload（按 user 隔离，无跨用户竞态）

        # 连接级资源上限（每用户独立连接，互不影响）；超限 DuckDB 落盘临时文件而非 OOM
        limits = _duckdb_limits()
        self._max_result_rows = limits["max_result_rows"]
        self._query_timeout = limits["query_timeout_seconds"]
        conn_config = {}
        if limits["memory_limit"]:
            conn_config["memory_limit"] = limits["memory_limit"]
        if limits["threads"] > 0:
            conn_config["threads"] = limits["threads"]
        self.conn = duckdb.connect(
            database=":memory:", config=conn_config or None
        )
        self._profile_cache: dict = {}

        if csv_path and os.path.exists(csv_path):
            self._load_csv(csv_path)
        logger.info(f"DuckDBManager initialized (user={user_id}) with table '{self.table_name}'")

    def _invalidate_profile(self, table_name: str) -> None:
        """表结构变更后清除该表的语义画像缓存，避免 get_enhanced_schema_text 命中 stale profile。"""
        self._profile_cache.pop(table_name, None)

    def _load_csv(self, csv_path: str):
        """Load CSV file into DuckDB as a table.（管理通道，不经查询白名单）"""
        qname = safe_ident(self.table_name)
        try:
            validate_table_name(self.table_name)
            validate_csv_path(csv_path)
            self.conn.execute(
                f"CREATE TABLE {qname} AS SELECT * FROM read_csv_auto('{csv_path}')"
            )
            row_count = self.conn.execute(
                f"SELECT COUNT(*) FROM {qname}"
            ).fetchone()[0]
            logger.info(f"Loaded {row_count} rows from {csv_path} into table '{self.table_name}'")
            self.last_loaded_csv = csv_path
            # 自动提取并持久化客户数据
            persist_customer_profiles(self.conn, self.table_name, self.user_id)
        except Exception as e:
            # read_csv_auto 默认 UTF-8，遇 GBK/GB18030 等非 UTF-8 中文 CSV 会因
            # "Invalid unicode" 失败。回退到 pandas 多编码解码（与 load_csv_dataset 一致）。
            logger.warning(f"_load_csv: read_csv_auto failed ({e}); trying pandas fallback")
            fallback_err = self._load_csv_via_pandas(csv_path, self.table_name)
            if fallback_err is not None:
                logger.error(f"Failed to load CSV {csv_path}: {fallback_err}")
                raise
            row_count = self.conn.execute(
                f"SELECT COUNT(*) FROM {qname}"
            ).fetchone()[0]
            logger.info(f"Loaded {row_count} rows from {csv_path} into table '{self.table_name}' (pandas fallback)")
            self.last_loaded_csv = csv_path
            persist_customer_profiles(self.conn, self.table_name, self.user_id)

    def execute(self, sql: str) -> duckdb.DuckDBPyRelation:
        """Execute a SQL query and return the DuckDB relation.

        查询通道：执行前做只读白名单校验，拦截 DROP/CREATE/INSERT 等写操作；
        执行期带超时 watchdog（Timer + conn.interrupt），超时抛 TimeoutError。
        管理通道（_load_csv/reload_csv）直接调 self.conn.execute，不经此校验。
        """
        assert_read_only(sql)
        logger.debug(f"Executing SQL: {sql[:200]}...")
        if self._query_timeout and self._query_timeout > 0:
            # DuckDB 无 SQL 级查询超时设置；Timer 线程超时 interrupt 执行中的
            # 查询，conn.execute 抛 InterruptException，转可读错误供 _fix_sql 回灌
            timer = threading.Timer(self._query_timeout, self._interrupt_conn)
            timer.daemon = True
            timer.start()
            try:
                return self.conn.execute(sql)
            except duckdb.InterruptException:
                raise TimeoutError(
                    f"查询超过 {self._query_timeout:g}s 超时上限已中断，"
                    f"请缩小扫描范围或添加过滤条件"
                )
            finally:
                timer.cancel()
        return self.conn.execute(sql)

    def _interrupt_conn(self) -> None:
        """watchdog 回调：中断当前连接上执行中的查询（连接空闲时为无害 no-op）。"""
        try:
            self.conn.interrupt()
        except Exception:
            pass

    def query_df(self, sql: str) -> pd.DataFrame:
        """Execute SQL and return results as a pandas DataFrame.

        结果行数超上限时抛 ValueError：SQLAgent 的错误回灌路径会把该消息
        交给 _fix_sql 重新生成带 LIMIT 的查询（自愈，无需额外通道）。
        """
        df = self.execute(sql).df()
        if self._max_result_rows and len(df) > self._max_result_rows:
            raise ValueError(
                f"查询结果 {len(df)} 行超过上限 {self._max_result_rows} 行，"
                f"请添加 LIMIT 或缩小查询范围"
            )
        return df

    def get_schema_text(self) -> str:
        """Return a human-readable schema description."""
        from database.schema import get_schema_text as _schema_text

        return _schema_text(self.conn)

    def get_table_names(self) -> list[str]:
        """Return list of table names in the database."""
        return [row[0] for row in self.execute("SHOW TABLES").fetchall()]

    def reload_csv(self, csv_path: str, table_name: str = "transactions"):
        """重新加载不同的 CSV 数据集到数据库（先删除旧表再创建新表）。（管理通道）

        本方法作用于本实例连接（按 user_id 隔离，无跨用户竞态）。
        """
        if not csv_path or not os.path.exists(csv_path):
            logger.warning(f"DuckDBManager.reload_csv: file not found: {csv_path}")
            return False
        # 若本实例已加载同一 CSV，无需重复 reload
        if self.last_loaded_csv == csv_path:
            return True
        try:
            validate_table_name(table_name)
            validate_csv_path(csv_path)
            qname = safe_ident(table_name)
            # 删除旧表前先清画像缓存,避免 reload 后 get_enhanced_schema_text 命中 stale profile
            self._invalidate_profile(table_name)
            # 删除旧表
            self.conn.execute(f"DROP TABLE IF EXISTS {qname}")
            # 加载新数据（_load_csv 内部会校验 self.table_name，故先同步实例属性）
            self.table_name = table_name
            self._load_csv(csv_path)
            logger.info(f"DuckDBManager reloaded with {csv_path}")
            return True
        except Exception as e:
            logger.error(f"DuckDBManager.reload_csv failed: {e}")
            return False

    def _clean_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """对 pandas 读取的 DataFrame 做结构清理 + 字符串值归一。

        - 丢弃全空行、全空列、跳过开头连续空行（应对合并单元格/空行造成的结构混乱）
        - object 列做 HTML 实体解码 + NBSP->空格 + 去零宽/BOM（仅显示层杂质，不动真实数值数据）
        """
        if df is None or df.empty:
            return df
        # 跳过开头连续全空行
        df = df.dropna(axis=0, how="all").reset_index(drop=True)
        # 丢弃全空列
        df = df.dropna(axis=1, how="all")
        # object 列值归一
        obj_cols = df.select_dtypes(include=["object"]).columns
        for c in obj_cols:
            df[c] = df[c].map(lambda v: self._clean_cell(v) if isinstance(v, str) else v)
        return df

    @staticmethod
    def _clean_cell(v: str) -> str:
        """清洗单个字符串值：HTML 实体解码 + NFKC(全角/NBSP) + 去不可见/控制字符 + 折叠空白。"""
        s = html.unescape(v)
        s = unicodedata.normalize("NFKC", s)
        s = _INVISIBLE_RE.sub("", s)
        s = _CONTROL_RE.sub("", s)
        s = _WS_RE.sub(" ", s).strip()
        return s

    def _normalize_table_columns(self, table_name: str) -> None:
        """建表后将列名归一化：若任一列名经清洗后变化，则用别名重建表。

        对 read_csv_auto 原生路径与 pandas 路径统一适用。重名已在
        _normalize_column_names 内追加 _2/_3 处理，别名重建天然避免冲突。
        """
        validate_table_name(table_name)
        qname = safe_ident(table_name)
        cols = [c[0] for c in self.conn.execute(f"DESCRIBE {qname}").fetchall()]
        new_cols = _normalize_column_names(cols)
        if new_cols == cols:
            return
        select_list = ", ".join(
            f"{safe_ident(old)} AS {safe_ident(new)}" for old, new in zip(cols, new_cols)
        )
        tmp = safe_ident("__norm_tmp")
        self.conn.execute(f"DROP TABLE IF EXISTS {tmp}")
        self.conn.execute(f"CREATE TABLE {tmp} AS SELECT {select_list} FROM {qname}")
        self.conn.execute(f"DROP TABLE {qname}")
        self.conn.execute(f"ALTER TABLE {tmp} RENAME TO {safe_ident(table_name)}")
        self._invalidate_profile(table_name)
        logger.info(f"_normalize_table_columns: cleaned {sum(a != b for a, b in zip(cols, new_cols))} column(s) in '{table_name}'")

    def load_csv_dataset(self, csv_path: str, table_name: str) -> dict:
        """加载 CSV 文件到指定表（管理通道，不经只读校验）。

        若表已存在则先 DROP 再重建。返回 {"success": bool, "row_count": int, "error": str|None}。
        """
        try:
            validate_table_name(table_name)
            validate_csv_path(csv_path)
            if not os.path.exists(csv_path):
                return {"success": False, "row_count": 0, "error": f"文件不存在: {csv_path}"}
            qname = safe_ident(table_name)
            self._invalidate_profile(table_name)
            self.conn.execute(f"DROP TABLE IF EXISTS {qname}")
            self.conn.execute(
                f"CREATE TABLE {qname} AS SELECT * FROM read_csv_auto('{csv_path}')"
            )
            # 列名归一：read_csv_auto 原生路径无 DataFrame，用别名重建清洗列名
            self._normalize_table_columns(table_name)
            row_count = self.conn.execute(
                f"SELECT COUNT(*) FROM {qname}"
            ).fetchone()[0]
            logger.info(f"load_csv_dataset: loaded {row_count} rows into '{table_name}' from {csv_path}")
            persist_customer_profiles(self.conn, table_name, self.user_id)
            return {"success": True, "row_count": row_count, "error": None}
        except Exception as e:
            # 回退：DuckDB read_csv_auto 默认 UTF-8，遇 GBK/GB18030 等非 UTF-8 中文 CSV
            # 会因 "Invalid unicode" 失败。改用 pandas 按候选编码解码为 DataFrame 再建表
            # （复用 load_excel_dataset 的 register+CREATE TABLE 模式，零扩展依赖）。
            logger.warning(f"load_csv_dataset: read_csv_auto failed for '{table_name}' ({e}); trying pandas fallback")
            fallback_err = self._load_csv_via_pandas(csv_path, table_name)
            if fallback_err is None:
                row_count = self.conn.execute(
                    f"SELECT COUNT(*) FROM {safe_ident(table_name)}"
                ).fetchone()[0]
                logger.info(f"load_csv_dataset: loaded {row_count} rows into '{table_name}' from {csv_path} (pandas fallback)")
                persist_customer_profiles(self.conn, table_name, self.user_id)
                return {"success": True, "row_count": row_count, "error": None}
            logger.error(f"load_csv_dataset failed for '{table_name}': {fallback_err}")
            return {"success": False, "row_count": 0, "error": fallback_err}

    def _load_csv_via_pandas(self, csv_path: str, table_name: str) -> str | None:
        """用 pandas 按 GBK/GB18030/UTF-8-SIG 解码 CSV 再建表。

        返回 None 表示成功；返回错误字符串表示失败。供 load_csv_dataset 回退。
        内部自算 qname，不依赖调用处的 qname（load_csv_dataset 可能在 qname 赋值前就抛）。
        """
        try:
            df = None
            for enc in ("gbk", "gb18030", "utf-8-sig", "utf-16"):
                try:
                    df = pd.read_csv(csv_path, encoding=enc)
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
            if df is None:
                return f"无法解码 CSV（已尝试 GBK/GB18030/UTF-8/UTF-16）：{csv_path}"
            if len(df.columns) == 0:
                return f"CSV 文件无有效数据列：{csv_path}"
            # 结构清理（去全空行列/前导空行）+ 字符串值归一
            df = self._clean_dataframe(df)
            if df is None or len(df.columns) == 0:
                return f"CSV 清洗后无有效数据列：{csv_path}"
            df.columns = _normalize_column_names(df.columns.tolist())
            qname = safe_ident(table_name)
            self._invalidate_profile(table_name)
            self.conn.execute(f"DROP TABLE IF EXISTS {qname}")
            tmp_view = f"__csv_load_{table_name}"
            self.conn.register(tmp_view, df)
            try:
                self.conn.execute(f"CREATE TABLE {qname} AS SELECT * FROM {tmp_view}")
            finally:
                self.conn.unregister(tmp_view)
            return None
        except Exception as pe:
            return f"pandas 回退失败: {pe}"

    def load_excel_dataset(self, excel_path: str, table_name: str, sheet: str | None = None) -> dict:
        """加载 Excel 文件到指定表（管理通道，不经只读校验）。

        若表已存在则先 DROP 再重建。sheet 参数可选，指定工作表名。
        返回 {"success": bool, "row_count": int, "error": str|None}。

        实现说明：不使用 DuckDB 的 read_excel()（依赖 spatial 扩展，需联网下载，
        在受限网络下会卡死/失败）。改用 pandas + openpyxl 读取为 DataFrame，
        再通过 con.register() 注册后建表，零扩展依赖、无需联网。
        """
        try:
            validate_table_name(table_name)
            validate_csv_path(excel_path)
            if not os.path.exists(excel_path):
                return {"success": False, "row_count": 0, "error": f"文件不存在: {excel_path}"}

            # pandas 读取 Excel（.xlsx/.xls 均支持；sheet_name 指定工作表，默认首张）
            read_kwargs = {}
            if sheet:
                read_kwargs["sheet_name"] = sheet
            df = pd.read_excel(excel_path, **read_kwargs)
            if df is None or len(df.columns) == 0:
                return {"success": False, "row_count": 0, "error": "Excel 文件无有效数据列"}

            # 结构清理（去全空行列/前导空行）+ 字符串值归一；列名归一
            df = self._clean_dataframe(df)
            if df is None or len(df.columns) == 0:
                return {"success": False, "row_count": 0, "error": "Excel 清洗后无有效数据列"}
            df.columns = _normalize_column_names(df.columns.tolist())
            qname = safe_ident(table_name)
            self._invalidate_profile(table_name)
            self.conn.execute(f"DROP TABLE IF EXISTS {qname}")
            # 用临时视图名注册 DataFrame，避免与用户表名冲突
            tmp_view = f"__excel_load_{table_name}"
            self.conn.register(tmp_view, df)
            try:
                self.conn.execute(f"CREATE TABLE {qname} AS SELECT * FROM {tmp_view}")
            finally:
                self.conn.unregister(tmp_view)

            row_count = self.conn.execute(
                f"SELECT COUNT(*) FROM {qname}"
            ).fetchone()[0]
            logger.info(f"load_excel_dataset: loaded {row_count} rows into '{table_name}' from {excel_path}")
            persist_customer_profiles(self.conn, table_name, self.user_id)
            return {"success": True, "row_count": row_count, "error": None}
        except Exception as e:
            logger.error(f"load_excel_dataset failed for '{table_name}': {e}")
            return {"success": False, "row_count": 0, "error": str(e)}

    def drop_table(self, table_name: str) -> bool:
        """删除指定表（管理通道，不经只读校验）。返回是否成功。"""
        try:
            validate_table_name(table_name)
            qname = safe_ident(table_name)
            self.conn.execute(f"DROP TABLE IF EXISTS {qname}")
            self._invalidate_profile(table_name)
            logger.info(f"drop_table: dropped '{table_name}'")
            return True
        except Exception as e:
            logger.error(f"drop_table failed for '{table_name}': {e}")
            return False

    def get_enhanced_schema_text(self, tables: list[str] | None = None,
                                  compact: bool = False,
                                  compact_sample_rows: int = 5) -> str:
        """增强版 schema 文本,含列语义统计(分类列取值/数值 min-max/宽表标记)。

        画像经实例级 _profile_cache 缓存,缓存缺失懒计算兜底。

        Args:
            tables: 可选表名白名单。None（默认）= 返回该连接内所有表（向后兼容，
                用于 test_schema_semantics.py 的旧调用与外部诊断工具）。传入
                list[str] 时只渲染这些表，其他表直接 skip——用于 SQL agent
                在 DataResolver 选出主数据集后只暴露目标表，避免 LLM 拿到
                同一用户的其它数据集（如「山东省人口」+ World Bank WDI 同时
                加载到同一连接时）被意外 JOIN 污染输出。
            compact: True 时输出精简版：仅列名 + 5 行 sample（替代全列统计
                /min-max/nunique 详尽输出），单表 schema 文本体量可降 50-70%。
                适用于 SQL agent 单表查询场景——LLM 拿到列名 + 真实 sample
                即可写出 SQL,详尽统计信息对单表查询是冗余（阶段 1.4 / 4.1
                优化目标,大表 5000-20000 字符 → ~1000-3000 字符）。
            compact_sample_rows: compact 模式下的 sample 行数（默认 5）。
        """
        from database import schema

        all_tables = self.get_table_names()
        if tables is not None:
            # 入参校验：复用 safe_ident/validate_table_name 防注入；只保留
            # 该连接中真实存在且在白名单的表（避免空过滤结果）。
            for t in tables:
                validate_table_name(t)
            allowed = {t for t in tables if t in all_tables}
            target_tables = [t for t in all_tables if t in allowed]
        else:
            target_tables = all_tables

        if not target_tables:
            return "No tables found."

        # 从 datasources_db 获取元数据映射 table_name -> {source_type, row_count}（仅本用户）
        meta_map: dict[str, dict] = {}
        try:
            from database.datasources_db import datasources_db
            for ds in datasources_db.list_datasets(owner_user_id=self.user_id):
                meta_map[ds["table_name"]] = {
                    "source_type": ds.get("source_type", "unknown"),
                    "row_count": ds.get("row_count", 0),
                }
        except Exception:
            logger.debug("get_enhanced_schema_text: datasources_db unavailable, using defaults")

        parts = []
        for table_name in target_tables:
            validate_table_name(table_name)
            # 懒计算（缓存已在 __init__ 初始化）
            if table_name not in self._profile_cache:
                try:
                    self._profile_cache[table_name] = schema.compute_table_profile(self.conn, table_name)
                except Exception as e:
                    logger.warning(f"compute_table_profile failed for '{table_name}': {e}")
                    self._profile_cache[table_name] = None
            profile = self._profile_cache[table_name]

            meta = meta_map.get(table_name, {})
            source_type = meta.get("source_type", "local")
            row_count = profile["row_count"] if profile else meta.get("row_count", 0)

            header = f"Table: {table_name} ({source_type}) [{row_count} rows]"
            if profile and profile.get("is_wide_table"):
                header += f"  [宽表:年份列 {profile['wide_table_range']}]"
            parts.append(header)

            if profile:
                if compact:
                    # 精简模式:仅列名(类型)+ 5 行 sample,省去 nunique/min-max
                    # 冗余输出(阶段 1.4 / 4.1:大表 schema 5000-20000 字符 → ~1000-3000)
                    cols_with_type = [
                        f"  - {c['name']} ({c['dtype']})"
                        for c in profile["columns"]
                    ]
                    parts.extend(cols_with_type)
                    if row_count and row_count > 0:
                        try:
                            sample = self.conn.execute(
                                f"SELECT * FROM {safe_ident(table_name)} LIMIT {int(compact_sample_rows)}"
                            ).fetchall()
                            col_names_list = [c["name"] for c in profile["columns"]]
                            parts.append(f"  示例前{min(len(sample), compact_sample_rows)}行:")
                            parts.append("    " + ", ".join(col_names_list))
                            for row in sample:
                                parts.append("    " + ", ".join(str(v) for v in row))
                        except Exception:
                            # sample 失败不影响主输出
                            pass
                else:
                    for c in profile["columns"]:
                        line = f"  - {c['name']} ({c['dtype']})"
                        if c.get("values") is not None:
                            vals = c["values"]
                            suffix = f"共{c['nunique']}个" if c["nunique"] > 8 else f"{c['nunique']}个"
                            line += f" — {suffix}唯一值: {vals}"
                            if c["nunique"] > 8:
                                line += " …"
                        elif c["nunique"] > 0:
                            line += f" — {c['nunique']}个唯一值"
                        if c.get("min") is not None:
                            line += f" (min={c['min']}, max={c['max']}, {c['non_null']}/{c['total']}非空)"
                        parts.append(line)
            else:
                # 画像失败兜底:回退到纯 DESCRIBE
                qname = safe_ident(table_name)
                cols = self.conn.execute(f"DESCRIBE {qname}").fetchall()
                for col_name, col_type, *_ in cols:
                    parts.append(f"  - {col_name} ({col_type})")
        return "\n".join(parts)

    def register_external_databases(self) -> dict:
        """注册外部数据库（实现属主 database/external_sources.py，见 R1 候选1 拆分）。"""
        return _register_external(self.conn)

    def close(self):
        """Close the DuckDB connection."""
        self.conn.close()
        logger.info("DuckDB connection closed")


# 按 user_id 缓存的 DuckDBManager 实例（每个 user 独立 :memory: 连接，互不干扰）。
# LRU 有上限（duckdb-instance-pool spec）：OrderedDict 记录访问新近度，超上限驱逐
# 最久未访问用户的实例（关闭连接）；同步路由在线程池并发触达，须加锁串行化
# move_to_end / popitem（非原子）。对齐 memory/short_term.py 的 SESSION_POOL_CAP 先例。
_duckdb_instances: "OrderedDict[str, DuckDBManager]" = OrderedDict()
_instances_lock = threading.Lock()

# 实例池上限默认值（config/agent.yml `duckdb.instance_pool_cap` 缺失时回退）
_DEFAULT_INSTANCE_POOL_CAP = 50


def _instance_pool_cap() -> int:
    """读取实例池 LRU 上限（防御式：配置缺失/非法回退默认值，下限 1）。"""
    cap = _DEFAULT_INSTANCE_POOL_CAP
    try:
        from utils.config_handler import agent_conf
        conf = (agent_conf or {}).get("duckdb", {}) or {}
        cap = int(conf.get("instance_pool_cap", _DEFAULT_INSTANCE_POOL_CAP))
    except Exception:
        pass
    return max(1, cap)


def _reload_datasets_into_instance(inst: "DuckDBManager") -> None:
    """将 datasources_db 中记录的、属于该实例用户的数据集重新加载到 DuckDB 实例中。

    按 inst.user_id 过滤，只加载该用户拥有的数据集（跨用户隔离），
    避免把 B 用户的私有数据集混入 A 的 DuckDB。
    仅加载文件类数据集（csv/excel），跳过外部数据库表（由 register_external_databases 处理）。
    失败不抛异常，仅记录日志。
    """
    try:
        from database.datasources_db import datasources_db
    except Exception:
        logger.debug("_reload_datasets_into_instance: datasources_db unavailable, skipping")
        return

    try:
        datasets = datasources_db.list_datasets(owner_user_id=inst.user_id)
    except Exception as e:
        logger.warning(f"_reload_datasets_into_instance: failed to list datasets: {e}")
        return

    for ds in datasets:
        source_type = ds.get("source_type", "")
        file_path = ds.get("file_path", "")
        table_name = ds.get("table_name", "")
        name = ds.get("name", "unknown")

        # 仅处理文件类数据集
        if source_type == "csv":
            result = inst.load_csv_dataset(file_path, table_name)
            if not result["success"]:
                logger.warning(f"_reload_datasets_into_instance: failed to reload CSV '{name}': {result.get('error')}")
        elif source_type == "excel":
            result = inst.load_excel_dataset(file_path, table_name)
            if not result["success"]:
                logger.warning(f"_reload_datasets_into_instance: failed to reload Excel '{name}': {result.get('error')}")
        # external db 类型由 register_external_databases 处理，此处跳过


def init_duckdb(csv_path: str | None = None, user_id: str = "default") -> DuckDBManager:
    """获取（或创建）指定 user_id 的 DuckDBManager 实例（LRU 池，有上限）。

    每个 user_id 拥有独立的 :memory: 连接和表，多用户并发不会互相覆盖数据。
    命中即提升新近度（LRU）；池超上限时驱逐最久未访问用户的实例（关闭连接），
    该用户下次访问经 _reload_datasets_into_instance 透明重建。
    若提供 csv_path 且与该实例上次加载的不同，会触发 reload。
    新建实例时会重新加载 datasources_db 中记录的所有数据集，并注册外部数据库连接。
    """
    if user_id is None:
        user_id = "default"

    if csv_path is None:
        from utils.path_tool import get_abs_path
        csv_path = get_abs_path("data/train.csv")

    with _instances_lock:
        inst = _duckdb_instances.get(user_id)
        if inst is None:
            inst = DuckDBManager(csv_path=csv_path, user_id=user_id)
            _duckdb_instances[user_id] = inst
            # 新建实例：重新加载所有已注册的数据集，并注册外部数据库连接
            _reload_datasets_into_instance(inst)
            try:
                inst.register_external_databases()
            except Exception as e:
                logger.warning(f"init_duckdb: register_external_databases failed for user={user_id}: {e}")
            # LRU 驱逐：池超上限时关最久未访问用户的实例（刚插入的在末端，不会被驱逐）
            cap = _instance_pool_cap()
            while len(_duckdb_instances) > cap:
                old_uid, old_inst = _duckdb_instances.popitem(last=False)
                try:
                    old_inst.close()
                except Exception as e:
                    logger.warning(f"init_duckdb: evict close failed for user={old_uid}: {e}")
                logger.info(f"init_duckdb: LRU evicted user={old_uid} (pool cap={cap})")
            needs_reload = False
        else:
            # 已有实例：刷新 LRU 新近度；若需要切换到不同 CSV 则 reload（锁外执行）
            _duckdb_instances.move_to_end(user_id)
            needs_reload = inst.last_loaded_csv != csv_path
    if needs_reload:
        inst.reload_csv(csv_path)
    return inst


def close_duckdb(user_id: str = "default") -> None:
    """关闭并移除指定 user 的 DuckDB 实例（资源清理，可选；语义与 LRU 池正交）。"""
    with _instances_lock:
        inst = _duckdb_instances.pop(user_id, None)
    if inst:
        inst.close()
