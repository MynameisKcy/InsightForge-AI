"""
DuckDB Manager: Load CSV data into DuckDB and provide query/execution interface.
"""

import os
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


class DuckDBManager:
    """Manages a DuckDB in-memory database, loads CSV data, and executes queries."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, csv_path: str | None = None, table_name: str = "transactions"):
        if self._initialized:
            return
        self._initialized = True

        self.conn = duckdb.connect(database=":memory:")
        self.table_name = table_name

        if csv_path and os.path.exists(csv_path):
            self._load_csv(csv_path)
        logger.info(f"DuckDBManager initialized with table '{self.table_name}'")

    def _load_csv(self, csv_path: str):
        """Load CSV file into DuckDB as a table."""
        try:
            self.conn.execute(
                f"CREATE TABLE {self.table_name} AS SELECT * FROM read_csv_auto('{csv_path}')"
            )
            row_count = self.conn.execute(
                f"SELECT COUNT(*) FROM {self.table_name}"
            ).fetchone()[0]
            logger.info(f"Loaded {row_count} rows from {csv_path} into table '{self.table_name}'")
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
            conn = sqlite3.connect(_CUSTOMER_DB_PATH)
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
        """Execute a SQL query and return the DuckDB relation."""
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
        """重新加载不同的 CSV 数据集到数据库（先删除旧表再创建新表）。"""
        if not csv_path or not os.path.exists(csv_path):
            logger.warning(f"DuckDBManager.reload_csv: file not found: {csv_path}")
            return False
        try:
            # 删除旧表
            self.conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            # 加载新数据
            self._load_csv(csv_path)
            self.table_name = table_name
            logger.info(f"DuckDBManager reloaded with {csv_path}")
            return True
        except Exception as e:
            logger.error(f"DuckDBManager.reload_csv failed: {e}")
            return False

    def close(self):
        """Close the DuckDB connection."""
        self.conn.close()
        DuckDBManager._instance = None
        self._initialized = False
        logger.info("DuckDB connection closed")


def init_duckdb(csv_path: str | None = None) -> DuckDBManager:
    """Initialize DuckDB with the default CSV dataset."""
    if csv_path is None:
        from utils.path_tool import get_abs_path

        csv_path = get_abs_path("data/train.csv")
    return DuckDBManager(csv_path=csv_path)


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
