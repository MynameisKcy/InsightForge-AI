"""
Datasources DB: SQLite 元数据管理，记录所有已注册的数据集（CSV/Excel/数据库表）。
"""

import os
import re
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

_DB_PATH = get_abs_path("database/datasources.db")

# 合法表名/数据集名：字母/下划线开头，仅含字母数字下划线（与 duckdb_manager._TABLE_NAME_RE 一致）
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DatasourcesDB:
    """数据源元数据管理。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or _DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_tables()

    @contextmanager
    def _get_conn(self):
        """获取 SQLite 连接（自动关闭）。"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_tables(self):
        with self._get_conn() as conn:
            # 新建表（若不存在）：name 不加列级 UNIQUE，改为 (owner_user_id, name) 联合唯一，
            # 这样不同用户可同名数据集（多用户隔离），同用户内仍防重。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    schema_json TEXT NOT NULL DEFAULT '[]',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    description TEXT NOT NULL DEFAULT '',
                    owner_user_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            # 迁移：为旧表补 owner_user_id 列（已有表时 ALTER）
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(datasets)").fetchall()}
            if "owner_user_id" not in cols:
                conn.execute(
                    "ALTER TABLE datasets ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''"
                )
                logger.info("DatasourcesDB: migrated datasets table — added owner_user_id column")

            # 迁移：旧表 name 带列级 UNIQUE 时，与多用户隔离冲突（跨用户同名触发 IntegrityError）。
            # SQLite 无法直接 DROP 列约束，需重建表去掉它（保留数据，联合唯一由下方
            # CREATE UNIQUE INDEX idx_datasets_owner_name 提供）。
            # 检测：查 origin='u'（表定义派生）的唯一约束，若存在「仅 name 单列」的则为旧表。
            # fresh 表无 'u' 约束、已迁移表只有 'c' 命名索引 -> 均不重建，避免每次启动空跑。
            needs_rebuild = False
            try:
                for ir in conn.execute("PRAGMA index_list('datasets')").fetchall():
                    if ir["origin"] == "u":  # u = 表定义派生的唯一约束
                        cols = [c["name"] for c in conn.execute(
                            "PRAGMA index_info('{}')".format(ir["name"])
                        ).fetchall()]
                        if cols == ["name"]:  # 单列 name 唯一 = 旧列级 UNIQUE
                            needs_rebuild = True
                            break
            except Exception:
                needs_rebuild = True

            if needs_rebuild:
                try:
                    conn.executescript("""
                        BEGIN;
                        CREATE TABLE datasets_new (
                            id TEXT PRIMARY KEY,
                            name TEXT NOT NULL,
                            source_type TEXT NOT NULL,
                            file_path TEXT NOT NULL,
                            table_name TEXT NOT NULL,
                            schema_json TEXT NOT NULL DEFAULT '[]',
                            row_count INTEGER NOT NULL DEFAULT 0,
                            description TEXT NOT NULL DEFAULT '',
                            owner_user_id TEXT NOT NULL DEFAULT '',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );
                        INSERT OR IGNORE INTO datasets_new
                            (id, name, source_type, file_path, table_name, schema_json,
                             row_count, description, owner_user_id, created_at, updated_at)
                        SELECT id, name, source_type, file_path, table_name, schema_json,
                               row_count, description, owner_user_id, created_at, updated_at
                        FROM datasets;
                        DROP TABLE datasets;
                        ALTER TABLE datasets_new RENAME TO datasets;
                        COMMIT;
                    """)
                    logger.info("DatasourcesDB: rebuilt datasets table — removed column-level UNIQUE on name, added UNIQUE(owner_user_id, name)")
                except Exception as e:
                    # 回滚并继续（不阻断启动）；联合唯一缺失会退化为"跨用户同名仍可能冲突"，
                    # 但上传端点 1b 会检查 add_dataset 返回值并提示，不会静默吞错。
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    logger.warning(f"DatasourcesDB: rebuild datasets table failed (non-fatal): {e}")

            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_datasets_owner_name ON datasets(owner_user_id, name)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_datasets_name ON datasets(name)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_datasets_table_name ON datasets(table_name)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_datasets_owner ON datasets(owner_user_id)
            """)
            conn.commit()

    def add_dataset(self, name: str, source_type: str, file_path: str,
                    table_name: str, schema_json: str, row_count: int,
                    description: str = "", owner_user_id: str = "") -> dict:
        if not name or not _TABLE_NAME_RE.match(name):
            raise ValueError(f"非法数据集名: {name!r}")
        if not table_name or not _TABLE_NAME_RE.match(table_name):
            raise ValueError(f"非法表名: {table_name!r}")
        try:
            ds_id = str(uuid.uuid4())
            now = datetime.now().isoformat()
            with self._get_conn() as conn:
                conn.execute(
                    """INSERT INTO datasets (id, name, source_type, file_path, table_name,
                       schema_json, row_count, description, owner_user_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ds_id, name, source_type, file_path, table_name,
                     schema_json, row_count, description, owner_user_id, now, now),
                )
                conn.commit()
            logger.info(f"DatasourcesDB: added dataset '{name}' (owner={owner_user_id}, type={source_type}, rows={row_count})")
            return {"success": True, "id": ds_id}
        except sqlite3.IntegrityError:
            return {"success": False, "error": f"数据集 '{name}' 已存在"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_dataset(self, name: str, owner_user_id: str | None = None) -> dict | None:
        """查询数据集。若提供 owner_user_id，则仅在该用户归属范围内查找（隔离）。

        owner_user_id=None 表示不做归属过滤（仅管理/迁移用途，业务端点不应使用）。
        """
        with self._get_conn() as conn:
            if owner_user_id is None:
                row = conn.execute("SELECT * FROM datasets WHERE name = ?", (name,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM datasets WHERE name = ? AND owner_user_id = ?",
                    (name, owner_user_id),
                ).fetchone()
            return dict(row) if row else None

    def list_datasets(self, owner_user_id: str | None = None) -> list[dict]:
        """列出数据集。若提供 owner_user_id，仅返回该用户拥有的数据集（隔离）。"""
        with self._get_conn() as conn:
            if owner_user_id is None:
                rows = conn.execute(
                    "SELECT * FROM datasets ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM datasets WHERE owner_user_id = ? ORDER BY created_at DESC",
                    (owner_user_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def delete_dataset(self, name: str, owner_user_id: str | None = None) -> dict:
        """删除数据集。若提供 owner_user_id，则必须归属匹配才会删除（防越权）。"""
        with self._get_conn() as conn:
            if owner_user_id is None:
                cursor = conn.execute("DELETE FROM datasets WHERE name = ?", (name,))
            else:
                cursor = conn.execute(
                    "DELETE FROM datasets WHERE name = ? AND owner_user_id = ?",
                    (name, owner_user_id),
                )
            conn.commit()
            if cursor.rowcount == 0:
                return {"success": False, "error": f"数据集 '{name}' 不存在或不属于该用户"}
        logger.info(f"DatasourcesDB: deleted dataset '{name}' (owner={owner_user_id})")
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

    def get_all_table_names(self, owner_user_id: str | None = None) -> list[str]:
        """获取数据集对应的表名列表。若提供 owner_user_id 则只返回该用户的（隔离）。"""
        with self._get_conn() as conn:
            if owner_user_id is None:
                rows = conn.execute("SELECT table_name FROM datasets").fetchall()
            else:
                rows = conn.execute(
                    "SELECT table_name FROM datasets WHERE owner_user_id = ?",
                    (owner_user_id,),
                ).fetchall()
            return [r["table_name"] for r in rows]


# 全局单例
datasources_db = DatasourcesDB()
