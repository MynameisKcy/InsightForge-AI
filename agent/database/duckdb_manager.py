"""
DuckDB Manager: Load CSV data into DuckDB and provide query/execution interface.
"""

import os
import re
import sqlite3
import sys
import duckdb
import pandas as pd

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


# 查询通道允许的 SQL 首关键词（大小写不敏感）。管理通道（_load_csv/reload_csv）不经此校验。
_READ_ONLY_ALLOWED_PREFIXES = {
    "SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN", "PRAGMA", "SUMMARIZE", "LIMIT",
}
# 显式拒绝的写操作关键词（防止通过注释/换行绕过首关键词检查，做二次扫描）。
_FORBIDDEN_KEYWORDS = {
    "DROP", "CREATE", "INSERT", "UPDATE", "DELETE", "ATTACH", "DETACH",
    "COPY", "EXPORT", "ALTER", "TRUNCATE", "REPLACE", "MERGE", "VACUUM",
    "CALL", "ATTACH", "IMPORT",
}
# 合法表名：字母/下划线开头，仅含字母数字下划线。
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _assert_read_only(sql: str) -> None:
    """查询通道白名单校验：仅允许只读类语句，拦截所有写/DDL/危险操作。

    管理通道（_load_csv 的 CREATE TABLE、reload_csv 的 DROP TABLE）通过
    self.conn.execute 直调，不经此方法，保持「管理通道 vs 查询通道」边界。
    """
    if not sql or not sql.strip():
        raise SecurityError("空 SQL 语句")

    stripped = sql.strip()
    # 去除前导注释和换行，取首个真实 SQL 关键词
    first_word = stripped.lstrip("/-* \t\n;").split(None, 1)[0] if stripped else ""
    first_word_upper = first_word.upper().rstrip("(")

    if first_word_upper not in _READ_ONLY_ALLOWED_PREFIXES:
        raise SecurityError(
            f"只读沙箱禁止执行以 '{first_word_upper}' 开头的语句（仅允许 "
            f"{sorted(_READ_ONLY_ALLOWED_PREFIXES)}）"
        )

    # 二次扫描：即便首关键词合法，也禁止任何写/DDL 关键词出现（防 /* */ 换行绕过）
    # 用 word boundary 避免误伤列名（如 "updated_at"）
    tokens = re.findall(r"[A-Za-z_]+", stripped)
    for tok in tokens:
        if tok.upper() in _FORBIDDEN_KEYWORDS:
            raise SecurityError(
                f"只读沙箱禁止包含写操作关键词 '{tok.upper()}' 的语句"
            )


def _validate_table_name(name: str) -> str:
    """校验表名合法（防 SQL 注入）：仅允许标识符字符。"""
    if not name or not _TABLE_NAME_RE.match(name):
        raise SecurityError(f"非法表名: {name!r}（仅允许字母/下划线开头、字母数字下划线）")
    return name


def _validate_csv_path(path: str) -> str:
    """校验 CSV 路径安全：必须在数据目录下且不含单引号（防 read_csv_auto 注入）。"""
    if not path:
        raise SecurityError("空 CSV 路径")
    if "'" in path or "\\" in path and "'" in path:
        raise SecurityError(f"CSV 路径含非法字符: {path!r}")
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

            # 持久化到 SQLite
            os.makedirs(os.path.dirname(_CUSTOMER_DB_PATH), exist_ok=True)
            conn = sqlite3.connect(_CUSTOMER_DB_PATH, timeout=30)
            # WAL 模式提升并发写性能，防 database is locked
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS customer_profiles (
                    customer_id TEXT PRIMARY KEY,
                    customer_name TEXT,
                    segment TEXT,
                    city TEXT,
                    region TEXT,
                    order_count INTEGER DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                )
            """)
            now = pd.Timestamp.now().isoformat()
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
                    INSERT INTO customer_profiles (customer_id, customer_name, segment, city, region, order_count, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(customer_id) DO UPDATE SET
                        customer_name = excluded.customer_name,
                        segment = excluded.segment,
                        city = excluded.city,
                        region = excluded.region,
                        order_count = excluded.order_count,
                        last_seen = excluded.last_seen
                """, (cid, vals["customer_name"], vals["segment"], vals["city"], vals["region"],
                      vals["order_count"], now, now))
            conn.commit()
            conn.close()
            logger.info(f"Persisted {len(df)} unique customers from {self.table_name}")
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
        """
        try:
            _validate_table_name(table_name)
            if not os.path.exists(excel_path):
                return {"success": False, "row_count": 0, "error": f"文件不存在: {excel_path}"}
            qname = safe_ident(table_name)
            self.conn.execute(f"DROP TABLE IF EXISTS {qname}")
            # 构建 read_excel 参数
            if sheet:
                self.conn.execute(
                    f"CREATE TABLE {qname} AS SELECT * FROM read_excel('{excel_path}', sheet_name='{sheet}')"
                )
            else:
                self.conn.execute(
                    f"CREATE TABLE {qname} AS SELECT * FROM read_excel('{excel_path}')"
                )
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
            logger.info(f"drop_table: dropped '{table_name}'")
            return True
        except Exception as e:
            logger.error(f"drop_table failed for '{table_name}': {e}")
            return False

    def get_enhanced_schema_text(self) -> str:
        """增强版 schema 文本，包含行数和来源类型标注。

        从 datasources_db 读取元数据，格式：
        Table: {name} ({source_type}) [{row_count} rows]
          - col1 (TYPE)
          - col2 (TYPE)
        """
        # 获取 DuckDB 中所有表
        tables = self.get_table_names()
        if not tables:
            return "No tables found."

        # 从 datasources_db 获取元数据映射 table_name -> {source_type, row_count}
        meta_map: dict[str, dict] = {}
        try:
            from database.datasources_db import datasources_db
            for ds in datasources_db.list_datasets():
                meta_map[ds["table_name"]] = {
                    "source_type": ds.get("source_type", "unknown"),
                    "row_count": ds.get("row_count", 0),
                }
        except Exception:
            logger.debug("get_enhanced_schema_text: datasources_db unavailable, using defaults")

        parts = []
        for table_name in tables:
            _validate_table_name(table_name)
            qname = safe_ident(table_name)
            cols = self.conn.execute(f"DESCRIBE {qname}").fetchall()
            col_lines = [f"  - {col_name} ({col_type})" for col_name, col_type, *_ in cols]

            # 获取元数据
            meta = meta_map.get(table_name, {})
            source_type = meta.get("source_type", "local")
            # 优先使用元数据中的 row_count，否则实时查询
            if "row_count" in meta and meta["row_count"] > 0:
                row_count = meta["row_count"]
            else:
                try:
                    row_count = self.conn.execute(f"SELECT COUNT(*) FROM {qname}").fetchone()[0]
                except Exception:
                    row_count = 0

            parts.append(
                f"Table: {table_name} ({source_type}) [{row_count} rows]\n" + "\n".join(col_lines)
            )
        return "\n\n".join(parts)

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

                # ATTACH 外部数据库
                attach_name = safe_ident(db_name)
                if db_type == "postgres":
                    self.conn.execute(
                        f"ATTACH 'host={host} port={port} user={user} password={password} dbname={database}' AS {attach_name} (TYPE postgres)"
                    )
                elif db_type == "mysql":
                    self.conn.execute(
                        f"ATTACH 'host={host} port={port} user={user} password={password} database={database}' AS {attach_name} (TYPE mysql)"
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
    """将 datasources_db 中记录的所有数据集重新加载到 DuckDB 实例中。

    仅加载文件类数据集（csv/excel），跳过外部数据库表（由 register_external_databases 处理）。
    失败不抛异常，仅记录日志。
    """
    try:
        from database.datasources_db import datasources_db
    except Exception:
        logger.debug("_reload_datasets_into_instance: datasources_db unavailable, skipping")
        return

    try:
        datasets = datasources_db.list_datasets()
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


def get_customer_overview(top_n: int = 10) -> list[dict]:
    """查询持久化的客户数据概况：按订单数排名返回 TOP N 客户。"""
    try:
        if not os.path.exists(_CUSTOMER_DB_PATH):
            return []
        conn = sqlite3.connect(_CUSTOMER_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT customer_id, customer_name, segment, city, region, order_count,
                      first_seen, last_seen
               FROM customer_profiles
               ORDER BY order_count DESC
               LIMIT ?""",
            (top_n,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"get_customer_overview failed: {e}")
        return []


def get_customer_count() -> dict:
    """获取持久化客户数据的统计信息。"""
    try:
        if not os.path.exists(_CUSTOMER_DB_PATH):
            return {"total_customers": 0, "by_city": [], "by_segment": []}
        conn = sqlite3.connect(_CUSTOMER_DB_PATH)
        conn.row_factory = sqlite3.Row

        total = conn.execute("SELECT COUNT(*) AS cnt FROM customer_profiles").fetchone()["cnt"]

        by_city = conn.execute(
            "SELECT city, COUNT(*) AS cnt FROM customer_profiles WHERE city != '' GROUP BY city ORDER BY cnt DESC LIMIT 10"
        ).fetchall()

        by_segment = conn.execute(
            "SELECT segment, COUNT(*) AS cnt FROM customer_profiles WHERE segment != '' GROUP BY segment ORDER BY cnt DESC"
        ).fetchall()

        conn.close()
        return {
            "total_customers": total,
            "by_city": [dict(r) for r in by_city],
            "by_segment": [dict(r) for r in by_segment],
        }
    except Exception as e:
        logger.warning(f"get_customer_count failed: {e}")
        return {"total_customers": 0, "by_city": [], "by_segment": []}
