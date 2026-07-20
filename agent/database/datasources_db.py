"""
Datasources DB: SQLite 元数据管理，记录所有已注册的数据集（CSV/Excel/数据库表）。
"""

import os
import sqlite3
import sys
import uuid
from datetime import datetime

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

_DB_PATH = get_abs_path("database/datasources.db")


class DatasourcesDB:
    """数据源元数据管理。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or _DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    source_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    schema_json TEXT NOT NULL DEFAULT '[]',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_datasets_name ON datasets(name)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_datasets_table_name ON datasets(table_name)
            """)
            conn.commit()

    def add_dataset(self, name: str, source_type: str, file_path: str,
                    table_name: str, schema_json: str, row_count: int,
                    description: str = "") -> dict:
        if not name or not table_name:
            return {"success": False, "error": "name 和 table_name 不能为空"}
        try:
            ds_id = str(uuid.uuid4())
            now = datetime.now().isoformat()
            with self._get_conn() as conn:
                conn.execute(
                    """INSERT INTO datasets (id, name, source_type, file_path, table_name,
                       schema_json, row_count, description, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ds_id, name, source_type, file_path, table_name,
                     schema_json, row_count, description, now, now),
                )
                conn.commit()
            logger.info(f"DatasourcesDB: added dataset '{name}' (type={source_type}, rows={row_count})")
            return {"success": True, "id": ds_id}
        except sqlite3.IntegrityError:
            return {"success": False, "error": f"数据集 '{name}' 已存在"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_dataset(self, name: str) -> dict | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM datasets WHERE name = ?", (name,)).fetchone()
            return dict(row) if row else None

    def list_datasets(self) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM datasets ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_dataset(self, name: str) -> dict:
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM datasets WHERE name = ?", (name,))
            conn.commit()
            if cursor.rowcount == 0:
                return {"success": False, "error": f"数据集 '{name}' 不存在"}
        logger.info(f"DatasourcesDB: deleted dataset '{name}'")
        return {"success": True}

    def update_dataset(self, name: str, **kwargs) -> dict:
        if not kwargs:
            return {"success": False, "error": "无更新字段"}
        allowed = {"source_type", "file_path", "table_name", "schema_json",
                   "row_count", "description"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return {"success": False, "error": "无有效更新字段"}
        updates["updated_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [name]
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"UPDATE datasets SET {set_clause} WHERE name = ?", values
            )
            conn.commit()
            if cursor.rowcount == 0:
                return {"success": False, "error": f"数据集 '{name}' 不存在"}
        return {"success": True}

    def get_all_table_names(self) -> list[str]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT table_name FROM datasets").fetchall()
            return [r["table_name"] for r in rows]


# 全局单例
datasources_db = DatasourcesDB()
