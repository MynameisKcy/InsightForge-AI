"""Customer Profiles — 从已加载数据集自动提取并持久化客户画像。

独立于 DuckDB：``persist_customer_profiles`` 接收一个 DuckDB 连接作参（不 import duckdb），
按列名启发式探测客户列（customer_id / name / segment / city / region），聚合后写入
SQLite ``customer_profiles``（按 ``user_id`` 隔离，复合主键 (customer_id, user_id)）。
``get_customer_overview`` / ``get_customer_count`` 只读 SQLite，供客户工具查询。

非客户数据集（无 customer 列）在探测阶段早退，代价可忽略。提取失败不影响调用方
（异常被吞并记日志，上传/加载照常成功）。
"""

import os
import sqlite3

import pandas as pd

from utils.logger_handler import logger

# 客户数据持久化 SQLite 路径
_CUSTOMER_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "customers.db")


def persist_customer_profiles(conn, table_name: str, user_id: str) -> None:
    """从已加载的 DuckDB 表中提取唯一客户数据，持久化到 SQLite。

    列名启发式探测客户列；无客户列则早退。异常被吞并（非关键），不影响调用方。
    """
    try:
        # 获取表列名（DuckDB information_schema）
        cols_df = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table_name],
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
            FROM {table_name}
            GROUP BY "{customer_id_col}"
        """
        df = conn.execute(query).df()

        if df.empty:
            return

        os.makedirs(os.path.dirname(_CUSTOMER_DB_PATH), exist_ok=True)
        sqlite_conn = sqlite3.connect(_CUSTOMER_DB_PATH, timeout=30)
        # WAL 模式提升并发写性能，防 database is locked
        try:
            sqlite_conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        sqlite_conn.execute("""
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
        cols = {r[1] for r in sqlite_conn.execute("PRAGMA table_info(customer_profiles)").fetchall()}
        if "user_id" not in cols:
            sqlite_conn.execute(
                "ALTER TABLE customer_profiles ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'"
            )
            logger.info("Migrated customer_profiles: added user_id column (legacy rows → 'default')")
        now = pd.Timestamp.now().isoformat()
        uid = user_id or "default"
        for _, row in df.iterrows():
            cid = str(row["customer_id"])
            vals = {
                "customer_name": str(row.get("customer_name", "")) if customer_name_col else "",
                "segment": str(row.get("segment", "")) if segment_col else "",
                "city": str(row.get("city", "")) if city_col else "",
                "region": str(row.get("region", "")) if region_col else "",
                "order_count": int(row.get("order_count", 0)),
            }
            sqlite_conn.execute("""
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
        sqlite_conn.commit()
        sqlite_conn.close()
        logger.info(f"Persisted {len(df)} unique customers from '{table_name}' (user={uid})")
    except Exception as e:
        logger.warning(f"Customer extraction skipped (non-critical): {e}")


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
