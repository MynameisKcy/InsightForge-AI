"""
DuckDB Manager: Load CSV data into DuckDB and provide query/execution interface.
"""

import os
import re
import sqlite3
import sys
import duckdb
import pandas as pd
import sqlglot
from sqlglot import exp

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.logger_handler import logger

# 客户数据持久化 SQLite 路径
_CUSTOMER_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "customers.db")


class SecurityError(Exception):
    """SQL 语句未通过只读沙箱白名单校验时抛出。"""


def safe_ident(name: str) -> str:
    """转义 DuckDB 标识符，防止 SQL 注入。"""
    return '"' + name.replace('"', '""') + '"'


# 查询通道允许的 SQL 语句类型（AST 节点 key）。仅允许只读 SELECT 派生类型，
# 以及只读 schema 探查语句（SHOW/DESCRIBE/SUMMARIZE）。
# 注意：EXPLAIN/LOAD/CALL/VACUUM 在 DuckDB 方言下会回退为 'command' 类型，
# 无法可靠校验内部，故 'command' 不在白名单——这些语句会被拒绝。
# 管理通道（_load_csv/reload_csv）不经此校验。
_READ_ONLY_STMT_TYPES = {
    "select", "union", "intersect", "except", "subquery",
    "show", "describe", "summarize",
}
# 显式拒绝的语句类型 key（DDL/DML/副作用类），双保险：即便上层放行也会拦下。
_FORBIDDEN_STMT_TYPES = {
    "create", "insert", "update", "delete", "drop", "alter", "truncate",
    "copy", "attach", "detach", "call", "set", "pragma", "vacuum",
    "merge", "replace", "install", "load",
}
# 禁用的函数名（规范化：去下划线大写）：任意文件读 / 网络访问 / 文件写入。
# 这些函数即使在 SELECT 内也会泄露文件内容或触发 SSRF/写盘，故全量禁止。
# 同时覆盖 sqlglot 的两种解析形态：Anonymous（read_csv_auto）与内置类（ReadCSV）。
_FORBIDDEN_FUNCTIONS = {
    # 文件读取
    "READCSVAUTO", "READCSV", "READJSON", "READJSONAUTO",
    "READPARQUET", "READBLOB", "READTEXT", "READTEXTAUTO",
    "READFWF", "READFWFAUTO",
    # 网络/远程
    "HTTPFS", "GLOB", "GLOBRECURSIVE",
    # DuckDB 扩展加载（不应在查询通道出现）
    "INSTALL", "LOAD",
    # 写文件（COPY/EXPORT 已在 stmt 层拦截，这里再防函数形态）
    "EXPORTDATABASE", "EXPORTPARQUET", "EXPORTCSV",
    # 执行外部
    "SYSTEM", "SHELL",
}
# 合法表名：字母/下划线开头，仅含字母数字下划线。
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_func_name(name: str) -> str:
    """规范化函数名：大写并去掉下划线，使 read_csv_auto / READCSV / ReadParquet
    等不同 sqlglot 解析形态（Anonymous vs 内置类）归一到同一可比较串。"""
    return (name or "").upper().replace("_", "")


def _collect_func_names(stmt: exp.Expression) -> set[str]:
    """收集 SQL AST 中出现的所有函数名（含匿名函数如 read_csv_auto），

    返回规范化后的集合（去掉下划线的大写名），如 {'READCSVAUTO', 'COUNT', 'SUM'}。
    sqlglot 对 read_csv_auto/read_json 解析为 exp.Anonymous（.name 带 _），
    对 read_csv/read_parquet 解析为内置类 ReadCSV/ReadParquet（类名无 _），
    故统一去下划线后比较。
    """
    names: set[str] = set()
    for f in stmt.find_all(exp.Func):
        if isinstance(f, exp.Anonymous):
            names.add(_normalize_func_name(f.name))
        else:
            names.add(_normalize_func_name(type(f).__name__))
    return names


def _assert_read_only(sql: str) -> None:
    """查询通道 AST 校验：仅允许只读 SELECT 派生语句，拦截写/DDL/文件/网络函数与多语句。

    使用 sqlglot 将 SQL 解析为 DuckDB 方言 AST，而非字符串关键词扫描，
    杜绝注释/换行/字符串拼接/函数构造等绕过手段。管理通道（_load_csv 的
    CREATE TABLE、reload_csv 的 DROP TABLE）通过 self.conn.execute 直调，
    不经此方法，保持「管理通道 vs 查询通道」边界。
    """
    if not sql or not sql.strip():
        raise SecurityError("空 SQL 语句")

    try:
        stmts = sqlglot.parse(sql, read="duckdb")
    except Exception as e:  # ParseError 等
        raise SecurityError(f"SQL 解析失败，拒绝执行: {type(e).__name__}: {e}")

    # 过滤掉纯空语句（仅注释等），保留真实语句
    real_stmts = [s for s in stmts if s is not None]
    if not real_stmts:
        raise SecurityError("SQL 无有效语句")
    if len(real_stmts) > 1:
        # 多语句（如 `SELECT 1; DROP TABLE x`）会解析为多条 AST，一律拒绝，防分号注入
        raise SecurityError(
            f"只读沙箱禁止多语句执行（检测到 {len(real_stmts)} 条语句）"
        )

    stmt = real_stmts[0]
    stmt_key = stmt.key.lower()

    # 1) 语句类型白名单
    if stmt_key in _FORBIDDEN_STMT_TYPES:
        raise SecurityError(f"只读沙箱禁止执行 '{stmt_key.upper()}' 语句")
    if stmt_key not in _READ_ONLY_STMT_TYPES:
        raise SecurityError(
            f"只读沙箱禁止以 '{stmt_key.upper()}' 开头的语句（仅允许只读 SELECT 派生）"
        )

    # 2) 函数级黑名单：即便语句是 SELECT，也禁止任何文件/网络/写盘函数
    func_names = _collect_func_names(stmt)
    bad_funcs = func_names & _FORBIDDEN_FUNCTIONS
    if bad_funcs:
        raise SecurityError(
            f"只读沙箱禁止调用文件/网络/写盘函数: {sorted(bad_funcs)}"
        )


def _validate_table_name(name: str) -> str:
    """校验表名合法（防 SQL 注入）：仅允许标识符字符。"""
    if not name or not _TABLE_NAME_RE.match(name):
        raise SecurityError(f"非法表名: {name!r}（仅允许字母/下划线开头、字母数字下划线）")
    return name


def _detect_wide_table(col_names: list[str]) -> tuple[bool, str | None]:
    """检测是否为宽表(≥5 个 4 位年份列名)。返回 (is_wide, 'YYYY-YYYY'|None)。"""
    import re
    year_cols = []
    for c in col_names:
        s = str(c).strip()
        if re.fullmatch(r"(19|20)\d{2}", s):
            year_cols.append(int(s))
    if len(year_cols) >= 5:
        year_cols.sort()
        return True, f"{year_cols[0]}-{year_cols[-1]}"
    return False, None


def _validate_csv_path(path: str) -> str:
    """校验数据文件路径安全：必须在 data 目录下且不含单引号（防 read_csv_auto 注入与路径穿越）。"""
    if not path:
        raise SecurityError("空数据文件路径")
    if "'" in path:
        raise SecurityError(f"数据文件路径含非法字符: {path!r}")
    # 路径穿越防护：realpath 必须在 data 目录下
    try:
        from utils.path_tool import get_abs_path
    except ModuleNotFoundError:
        from agent.utils.path_tool import get_abs_path
    allowed_root = os.path.realpath(get_abs_path("data"))
    real = os.path.realpath(path)
    if not real.startswith(allowed_root + os.sep) and real != allowed_root:
        raise SecurityError(f"数据文件路径越界: {path!r}")
    return path


class DuckDBManager:
    """Manages a DuckDB in-memory database, loads CSV data, and executes queries.

    不再是进程级单例：每个实例拥有独立的 :memory: 连接，按 user_id 隔离数据，
    避免多用户并发时互相覆盖表数据。通过 init_duckdb(user_id) 工厂按 user_id 缓存实例。
    """

    def __init__(self, csv_path: str | None = None, table_name: str = "transactions", user_id: str = "default"):
        _validate_table_name(table_name)
        self.user_id = user_id
        self.table_name = table_name
        self.last_loaded_csv: str | None = None  # 本实例上次加载的 CSV，用于判断是否需要 reload（按 user 隔离，无跨用户竞态）

        self.conn = duckdb.connect(database=":memory:")

        if csv_path and os.path.exists(csv_path):
            self._load_csv(csv_path)
        logger.info(f"DuckDBManager initialized (user={user_id}) with table '{self.table_name}'")

    def _load_csv(self, csv_path: str):
        """Load CSV file into DuckDB as a table.（管理通道，不经查询白名单）"""
        try:
            _validate_table_name(self.table_name)
            _validate_csv_path(csv_path)
            self.conn.execute(
                f"CREATE TABLE {self.table_name} AS SELECT * FROM read_csv_auto('{csv_path}')"
            )
            row_count = self.conn.execute(
                f"SELECT COUNT(*) FROM {self.table_name}"
            ).fetchone()[0]
            logger.info(f"Loaded {row_count} rows from {csv_path} into table '{self.table_name}'")
            self.last_loaded_csv = csv_path
            # 自动提取并持久化客户数据
            self._extract_and_persist_customers()
        except Exception as e:
            logger.error(f"Failed to load CSV {csv_path}: {e}")
            raise

    def _extract_and_persist_customers(self):
        """从已加载的 DuckDB 表中提取唯一客户数据，持久化到 SQLite。"""
        try:
            # 获取表列名（DuckDB information_schema）
            cols_df = self.conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [self.table_name],
            ).df()
            if cols_df.empty:
                return
            col_names = [c.lower().strip().replace('"', '') for c in cols_df["column_name"].tolist()]

            # 检测客户 ID 列
            customer_id_col = None
            for c in col_names:
                if "customer" in c and "id" in c:
                    customer_id_col = c
                    break
            # 备选：customer 开头的任意列
            if not customer_id_col:
                for c in col_names:
                    if c.startswith("customer"):
                        customer_id_col = c
                        break
            if not customer_id_col:
                return  # 无客户数据列，跳过

            # 检测其他客户相关列
            customer_name_col = None
            for c in col_names:
                if "customer" in c and "name" in c:
                    customer_name_col = c
                    break

            segment_col = None
            for c in col_names:
                if c in ("segment", "customer_segment", "cust_segment"):
                    segment_col = c
                    break

            city_col = None
            for c in col_names:
                if c in ("city", "customer_city"):
                    city_col = c
                    break

            region_col = None
            for c in col_names:
                if c in ("region", "state", "country"):
                    region_col = c
                    break

            # 构建 SELECT 列表
            select_parts = [f'"{customer_id_col}" AS customer_id']
            extra_cols = []
            if customer_name_col:
                select_parts.append(f'MAX("{customer_name_col}") AS customer_name')
                extra_cols.append("customer_name")
            if segment_col:
                select_parts.append(f'MAX("{segment_col}") AS segment')
                extra_cols.append("segment")
            if city_col:
                select_parts.append(f'MAX("{city_col}") AS city')
                extra_cols.append("city")
            if region_col:
                select_parts.append(f'MAX("{region_col}") AS region')
                extra_cols.append("region")

            select_sql = ", ".join(select_parts)
            query = f"""
                SELECT {select_sql}, COUNT(*) AS order_count
                FROM {self.table_name}
                GROUP BY "{customer_id_col}"
            """
            df = self.conn.execute(query).df()

            if df.empty:
                return

            os.makedirs(os.path.dirname(_CUSTOMER_DB_PATH), exist_ok=True)
            conn = sqlite3.connect(_CUSTOMER_DB_PATH, timeout=30)
            # WAL 模式提升并发写性能，防 database is locked
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS customer_profiles (
                    customer_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    customer_name TEXT,
                    segment TEXT,
                    city TEXT,
                    region TEXT,
                    order_count INTEGER DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    PRIMARY KEY (customer_id, user_id)
                )
            """)
            # 迁移：旧表无 user_id 列时补列（主键改造较复杂，对存量数据默认归属到 default 用户）
            cols = {r[1] for r in conn.execute("PRAGMA table_info(customer_profiles)").fetchall()}
            if "user_id" not in cols:
                conn.execute(
                    "ALTER TABLE customer_profiles ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'"
                )
                logger.info("Migrated customer_profiles: added user_id column (legacy rows → 'default')")
            now = pd.Timestamp.now().isoformat()
            uid = self.user_id or "default"
            for _, row in df.iterrows():
                cid = str(row["customer_id"])
                vals = {
                    "customer_name": str(row.get("customer_name", "")) if customer_name_col else "",
                    "segment": str(row.get("segment", "")) if segment_col else "",
                    "city": str(row.get("city", "")) if city_col else "",
                    "region": str(row.get("region", "")) if region_col else "",
                    "order_count": int(row.get("order_count", 0)),
                }
                conn.execute("""
                    INSERT INTO customer_profiles (customer_id, user_id, customer_name, segment, city, region, order_count, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(customer_id, user_id) DO UPDATE SET
                        customer_name = excluded.customer_name,
                        segment = excluded.segment,
                        city = excluded.city,
                        region = excluded.region,
                        order_count = excluded.order_count,
                        last_seen = excluded.last_seen
                """, (cid, uid, vals["customer_name"], vals["segment"], vals["city"], vals["region"],
                      vals["order_count"], now, now))
            conn.commit()
            conn.close()
            logger.info(f"Persisted {len(df)} unique customers from {self.table_name} (user={uid})")
        except Exception as e:
            logger.warning(f"Customer extraction skipped (non-critical): {e}")

    def execute(self, sql: str) -> duckdb.DuckDBPyRelation:
        """Execute a SQL query and return the DuckDB relation.

        查询通道：执行前做只读白名单校验，拦截 DROP/CREATE/INSERT 等写操作。
        管理通道（_load_csv/reload_csv）直接调 self.conn.execute，不经此校验。
        """
        _assert_read_only(sql)
        logger.debug(f"Executing SQL: {sql[:200]}...")
        return self.conn.execute(sql)

    def query_df(self, sql: str) -> pd.DataFrame:
        """Execute SQL and return results as a pandas DataFrame."""
        return self.execute(sql).df()

    def get_schema_text(self) -> str:
        """Return a human-readable schema description."""
        from database.schema_loader import SchemaLoader

        return SchemaLoader.get_schema_text(self.conn)

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
            _validate_table_name(table_name)
            _validate_csv_path(csv_path)
            # 删除旧表前先清画像缓存,避免 reload 后 get_enhanced_schema_text 命中 stale profile
            if hasattr(self, "_profile_cache"):
                self._profile_cache.pop(table_name, None)
            # 删除旧表
            self.conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            # 加载新数据（_load_csv 内部会校验 self.table_name，故先同步实例属性）
            self.table_name = table_name
            self._load_csv(csv_path)
            logger.info(f"DuckDBManager reloaded with {csv_path}")
            return True
        except Exception as e:
            logger.error(f"DuckDBManager.reload_csv failed: {e}")
            return False

    def load_csv_dataset(self, csv_path: str, table_name: str) -> dict:
        """加载 CSV 文件到指定表（管理通道，不经只读校验）。

        若表已存在则先 DROP 再重建。返回 {"success": bool, "row_count": int, "error": str|None}。
        """
        try:
            _validate_table_name(table_name)
            _validate_csv_path(csv_path)
            if not os.path.exists(csv_path):
                return {"success": False, "row_count": 0, "error": f"文件不存在: {csv_path}"}
            qname = safe_ident(table_name)
            if hasattr(self, "_profile_cache"):
                self._profile_cache.pop(table_name, None)
            self.conn.execute(f"DROP TABLE IF EXISTS {qname}")
            self.conn.execute(
                f"CREATE TABLE {qname} AS SELECT * FROM read_csv_auto('{csv_path}')"
            )
            row_count = self.conn.execute(
                f"SELECT COUNT(*) FROM {qname}"
            ).fetchone()[0]
            logger.info(f"load_csv_dataset: loaded {row_count} rows into '{table_name}' from {csv_path}")
            return {"success": True, "row_count": row_count, "error": None}
        except Exception as e:
            logger.error(f"load_csv_dataset failed for '{table_name}': {e}")
            return {"success": False, "row_count": 0, "error": str(e)}

    def load_excel_dataset(self, excel_path: str, table_name: str, sheet: str | None = None) -> dict:
        """加载 Excel 文件到指定表（管理通道，不经只读校验）。

        若表已存在则先 DROP 再重建。sheet 参数可选，指定工作表名。
        返回 {"success": bool, "row_count": int, "error": str|None}。

        实现说明：不使用 DuckDB 的 read_excel()（依赖 spatial 扩展，需联网下载，
        在受限网络下会卡死/失败）。改用 pandas + openpyxl 读取为 DataFrame，
        再通过 con.register() 注册后建表，零扩展依赖、无需联网。
        """
        try:
            _validate_table_name(table_name)
            _validate_csv_path(excel_path)
            if not os.path.exists(excel_path):
                return {"success": False, "row_count": 0, "error": f"文件不存在: {excel_path}"}

            # pandas 读取 Excel（.xlsx/.xls 均支持；sheet_name 指定工作表，默认首张）
            read_kwargs = {}
            if sheet:
                read_kwargs["sheet_name"] = sheet
            df = pd.read_excel(excel_path, **read_kwargs)
            if df is None or len(df.columns) == 0:
                return {"success": False, "row_count": 0, "error": "Excel 文件无有效数据列"}

            qname = safe_ident(table_name)
            if hasattr(self, "_profile_cache"):
                self._profile_cache.pop(table_name, None)
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
            return {"success": True, "row_count": row_count, "error": None}
        except Exception as e:
            logger.error(f"load_excel_dataset failed for '{table_name}': {e}")
            return {"success": False, "row_count": 0, "error": str(e)}

    def drop_table(self, table_name: str) -> bool:
        """删除指定表（管理通道，不经只读校验）。返回是否成功。"""
        try:
            _validate_table_name(table_name)
            qname = safe_ident(table_name)
            self.conn.execute(f"DROP TABLE IF EXISTS {qname}")
            if hasattr(self, "_profile_cache"):
                self._profile_cache.pop(table_name, None)
            logger.info(f"drop_table: dropped '{table_name}'")
            return True
        except Exception as e:
            logger.error(f"drop_table failed for '{table_name}': {e}")
            return False

    def _compute_table_profile(self, table_name: str) -> dict:
        """计算单表语义画像:每列 nunique/取值/数值统计 + 宽表标记。供 schema 文本与缓存使用。"""
        _validate_table_name(table_name)
        qname = safe_ident(table_name)
        cols = self.conn.execute(f"DESCRIBE {qname}").fetchall()
        col_names = [c[0] for c in cols]
        total = self.conn.execute(f"SELECT COUNT(*) FROM {qname}").fetchone()[0]
        is_wide, wide_range = _detect_wide_table(col_names)

        col_profiles = []
        for col_name, col_type, *_ in cols:
            is_numeric = col_type.upper() in ("DOUBLE", "FLOAT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "DECIMAL", "REAL", "HUGEINT")
            # nunique + 非空数
            agg = self.conn.execute(
                f'SELECT COUNT(DISTINCT "{col_name}"), COUNT("{col_name}") FROM {qname}'
            ).fetchone()
            nunique, non_null = int(agg[0]), int(agg[1])
            entry = {"name": col_name, "dtype": col_type, "nunique": nunique,
                     "non_null": non_null, "total": total}
            if is_numeric:
                if non_null > 0:
                    mm = self.conn.execute(
                        f'SELECT MIN("{col_name}"), MAX("{col_name}") FROM {qname} WHERE "{col_name}" IS NOT NULL'
                    ).fetchone()
                    entry["min"] = float(mm[0]) if mm[0] is not None else None
                    entry["max"] = float(mm[1]) if mm[1] is not None else None
            else:
                # 低基数分类列:列取值(最多 8 个)
                if 0 < nunique <= 15:
                    vals = self.conn.execute(
                        f'SELECT DISTINCT "{col_name}" FROM {qname} WHERE "{col_name}" IS NOT NULL LIMIT 8'
                    ).fetchall()
                    entry["values"] = [str(v[0]) for v in vals]
            col_profiles.append(entry)
        return {"columns": col_profiles, "is_wide_table": is_wide,
                "wide_table_range": wide_range, "row_count": total}

    def get_enhanced_schema_text(self) -> str:
        """增强版 schema 文本,含列语义统计(分类列取值/数值 min-max/宽表标记)。

        画像经实例级 _profile_cache 缓存,缓存缺失懒计算兜底。
        """
        tables = self.get_table_names()
        if not tables:
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
        for table_name in tables:
            _validate_table_name(table_name)
            # 缓存懒初始化 + 懒计算
            if not hasattr(self, "_profile_cache"):
                self._profile_cache = {}
            if table_name not in self._profile_cache:
                try:
                    self._profile_cache[table_name] = self._compute_table_profile(table_name)
                except Exception as e:
                    logger.warning(f"_compute_table_profile failed for '{table_name}': {e}")
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
        """读取 datasources_conf 配置，安装 DuckDB 扩展，注册外部数据库表为视图。

        返回 {"registered": [...], "failed": [...]}。
        失败时不会崩溃，仅记录错误并继续。
        """
        registered = []
        failed = []

        try:
            from utils.config_handler import datasources_conf
        except Exception:
            logger.info("register_external_databases: datasources_conf not available, skipping")
            return {"registered": registered, "failed": failed}

        if not datasources_conf or not datasources_conf.get("databases"):
            return {"registered": registered, "failed": failed}

        for db_conf in datasources_conf["databases"]:
            db_name = db_conf.get("name", "unknown")
            db_type = db_conf.get("type", "").lower()

            try:
                # 安装并加载对应扩展
                if db_type == "postgres":
                    self.conn.execute("INSTALL postgres_scan")
                    self.conn.execute("LOAD postgres_scan")
                elif db_type == "mysql":
                    self.conn.execute("INSTALL mysql_scan")
                    self.conn.execute("LOAD mysql_scan")
                else:
                    failed.append({"name": db_name, "error": f"不支持的数据库类型: {db_type}"})
                    continue

                # 读取密码（从环境变量）
                import os as _os
                password = _os.environ.get(db_conf.get("password_env", ""), "")

                # 构建连接参数
                host = db_conf.get("host", "127.0.0.1")
                port = db_conf.get("port", 5432 if db_type == "postgres" else 3306)
                database = db_conf.get("database", "")
                user = db_conf.get("user", "")

                # ATTACH 外部数据库（连接字符串中的单引号需转义，防 SQL 注入）
                attach_name = safe_ident(db_name)
                # 数值型 port 不转义；其余字段单引号需翻倍转义
                port_str = str(port) if str(port).isdigit() else str(port).replace("'", "''")
                host_e = str(host).replace("'", "''")
                user_e = str(user).replace("'", "''")
                password_e = str(password).replace("'", "''")
                database_e = str(database).replace("'", "''")
                if db_type == "postgres":
                    self.conn.execute(
                        f"ATTACH 'host={host_e} port={port_str} user={user_e} password={password_e} dbname={database_e}' AS {attach_name} (TYPE postgres)"
                    )
                elif db_type == "mysql":
                    self.conn.execute(
                        f"ATTACH 'host={host_e} port={port_str} user={user_e} password={password_e} database={database_e}' AS {attach_name} (TYPE mysql)"
                    )

                # 确定要暴露的表
                tables_list = db_conf.get("tables", [])
                if not tables_list:
                    # 自动发现：查询 information_schema
                    try:
                        if db_type == "postgres":
                            schema_rows = self.conn.execute(
                                f"SELECT table_name FROM {attach_name}.information_schema.tables WHERE table_schema='public'"
                            ).fetchall()
                        elif db_type == "mysql":
                            schema_rows = self.conn.execute(
                                f"SELECT table_name FROM {attach_name}.information_schema.tables WHERE table_schema=DATABASE()"
                            ).fetchall()
                        else:
                            schema_rows = []
                        tables_list = [r[0] for r in schema_rows]
                    except Exception as e:
                        logger.warning(f"register_external_databases: auto-discover tables failed for {db_name}: {e}")
                        tables_list = []

                # 为每个表创建视图
                for tbl in tables_list:
                    try:
                        view_name = safe_ident(tbl)
                        self.conn.execute(
                            f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM {attach_name}.{safe_ident(tbl)}"
                        )
                        registered.append({"database": db_name, "table": tbl})
                        logger.info(f"register_external_databases: registered view '{tbl}' from {db_name}")
                    except Exception as e:
                        failed.append({"name": f"{db_name}.{tbl}", "error": str(e)})
                        logger.warning(f"register_external_databases: failed to create view for {db_name}.{tbl}: {e}")

            except Exception as e:
                failed.append({"name": db_name, "error": str(e)})
                logger.warning(f"register_external_databases: failed for {db_name}: {e}")

        return {"registered": registered, "failed": failed}

    def close(self):
        """Close the DuckDB connection."""
        self.conn.close()
        logger.info("DuckDB connection closed")


# 按 user_id 缓存的 DuckDBManager 实例（每个 user 独立 :memory: 连接，互不干扰）
_duckdb_instances: dict[str, "DuckDBManager"] = {}


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
    """获取（或创建）指定 user_id 的 DuckDBManager 实例。

    每个 user_id 拥有独立的 :memory: 连接和表，多用户并发不会互相覆盖数据。
    若提供 csv_path 且与该实例上次加载的不同，会触发 reload。
    新建实例时会重新加载 datasources_db 中记录的所有数据集，并注册外部数据库连接。
    """
    if user_id is None:
        user_id = "default"

    if csv_path is None:
        from utils.path_tool import get_abs_path
        csv_path = get_abs_path("data/train.csv")

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
    else:
        # 已有实例：若需要切换到不同 CSV 则 reload
        if inst.last_loaded_csv != csv_path:
            inst.reload_csv(csv_path)
    return inst


def close_duckdb(user_id: str = "default") -> None:
    """关闭并移除指定 user 的 DuckDB 实例（资源清理，可选）。"""
    inst = _duckdb_instances.pop(user_id, None)
    if inst:
        inst.close()


def get_customer_overview(user_id: str, top_n: int = 10) -> list[dict]:
    """查询指定用户持久化的客户数据概况：按订单数排名返回 TOP N 客户。

    user_id 必填：只返回该用户上传数据集中提取的客户，跨用户隔离。
    """
    try:
        if not os.path.exists(_CUSTOMER_DB_PATH):
            return []
        conn = sqlite3.connect(_CUSTOMER_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT customer_id, customer_name, segment, city, region, order_count,
                      first_seen, last_seen
               FROM customer_profiles
               WHERE user_id = ?
               ORDER BY order_count DESC
               LIMIT ?""",
            (user_id or "default", top_n),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"get_customer_overview failed (user={user_id}): {e}")
        return []


def get_customer_count(user_id: str) -> dict:
    """获取指定用户持久化客户数据的统计信息（跨用户隔离）。"""
    try:
        if not os.path.exists(_CUSTOMER_DB_PATH):
            return {"total_customers": 0, "by_city": [], "by_segment": []}
        conn = sqlite3.connect(_CUSTOMER_DB_PATH)
        conn.row_factory = sqlite3.Row
        uid = user_id or "default"

        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM customer_profiles WHERE user_id = ?", (uid,)
        ).fetchone()["cnt"]

        by_city = conn.execute(
            "SELECT city, COUNT(*) AS cnt FROM customer_profiles WHERE user_id = ? AND city != '' GROUP BY city ORDER BY cnt DESC LIMIT 10",
            (uid,),
        ).fetchall()

        by_segment = conn.execute(
            "SELECT segment, COUNT(*) AS cnt FROM customer_profiles WHERE user_id = ? AND segment != '' GROUP BY segment ORDER BY cnt DESC",
            (uid,),
        ).fetchall()

        conn.close()
        return {
            "total_customers": total,
            "by_city": [dict(r) for r in by_city],
            "by_segment": [dict(r) for r in by_segment],
        }
    except Exception as e:
        logger.warning(f"get_customer_count failed (user={user_id}): {e}")
        return {"total_customers": 0, "by_city": [], "by_segment": []}
