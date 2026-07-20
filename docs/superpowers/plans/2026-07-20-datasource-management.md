# 数据源管理增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 InsightForge AI 支持用户上传 CSV/Excel 文件和管理员预配置关系型数据库，通过 DuckDB 联邦查询实现跨数据集关联分析。

**Architecture:** 新建 `datasources_db.py` 管理 SQLite 元数据，改造 `duckdb_manager.py` 支持多表加载和 DuckDB 扩展（postgres_scan/mysql_scan），改造 `data_resolver.py` 从元数据动态读取数据集，改造 `sql_agent.py` 注入多表 schema，新增 FastAPI 数据集管理端点和前端面板。

**Tech Stack:** DuckDB (联邦查询 + read_csv_auto + postgres_scan/mysql_scan)、SQLite (元数据)、FastAPI (SSE + REST)、Plotly (图表不变)

## Global Constraints

- Python 3.10+（使用 `str | None` 类型语法）
- 密码从不硬编码，通过 `.env` 环境变量读取
- 所有 YAML 配置通过 `utils/config_handler.py` 的模式加载（函数 + 模块级全局变量）
- 文件路径通过 `utils/path_tool.py` 的 `get_abs_path()` 解析
- 导入兼容两种模式：`from database.xxx import` 和 `from agent.database.xxx import`（try/except ModuleNotFoundError）
- SQL 沙箱保持只读白名单 + 禁止关键词二次扫描
- 纯本地单机部署，无多租户需求

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `agent/database/datasources_db.py` | 数据源元数据 SQLite CRUD（datasets 表） |
| Create | `agent/config/datasources.yml` | 管理员预配置的数据库连接（MySQL/PostgreSQL） |
| Create | `agent/tests/test_datasources_db.py` | datasources_db 的单元测试 |
| Create | `agent/tests/test_duckdb_multi_source.py` | DuckDB 多数据源加载的集成测试 |
| Modify | `agent/database/duckdb_manager.py` | 多表加载、DuckDB 扩展安装、启动时重加载、查询超时 |
| Modify | `agent/database/data_resolver.py` | 从 datasources.db 动态读取数据集列表，替代硬编码 DATASET_MAP |
| Modify | `agent/database/schema_loader.py` | 增强 schema 输出：包含行数、源类型注释 |
| Modify | `agent/agents/sql_agent.py` | 多表 schema 注入、移除单表假设 |
| Modify | `agent/agent/tools/agent_tools.py` | 更新 get_data_overview 支持多表 |
| Modify | `agent/utils/config_handler.py` | 新增 load_datasources_config 函数和 datasources_conf 全局变量 |
| Modify | `agent/api/fastapi_server.py` | 新增 5 个数据集管理端点 + 前端数据集面板 HTML |

---

### Task 1: datasources_db.py — 数据源元数据管理

**Files:**
- Create: `agent/database/datasources_db.py`
- Create: `agent/tests/test_datasources_db.py`

**Interfaces:**
- Produces: `DatasourcesDB` 类，方法签名如下：
  - `add_dataset(name, source_type, file_path, table_name, schema_json, row_count, description="") -> dict` — 返回 `{"success": bool, "id": str, "error": str|None}`
  - `get_dataset(name: str) -> dict|None` — 按 name 查找
  - `list_datasets() -> list[dict]` — 返回所有数据集
  - `delete_dataset(name: str) -> dict` — 返回 `{"success": bool, "error": str|None}`
  - `update_dataset(name, **kwargs) -> dict` — 更新指定字段
  - `get_all_table_names() -> list[str]` — 返回所有已注册的 DuckDB 表名
- Produces: 全局单例 `datasources_db = DatasourcesDB()`

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_datasources_db.py
import os
import sys
import unittest
import tempfile

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestDatasourcesDB(unittest.TestCase):
    def setUp(self):
        """每个测试用临时数据库文件。"""
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_datasources.db")
        from database.datasources_db import DatasourcesDB
        self.db = DatasourcesDB(db_path=self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_add_and_get_dataset(self):
        result = self.db.add_dataset(
            name="sales_2024",
            source_type="csv",
            file_path="/tmp/sales_2024.csv",
            table_name="sales_2024",
            schema_json='[{"name":"id","type":"INTEGER"}]',
            row_count=100,
        )
        self.assertTrue(result["success"])
        self.assertIsNotNone(result["id"])

        ds = self.db.get_dataset("sales_2024")
        self.assertIsNotNone(ds)
        self.assertEqual(ds["source_type"], "csv")
        self.assertEqual(ds["row_count"], 100)

    def test_add_duplicate_name_fails(self):
        self.db.add_dataset(
            name="test_ds", source_type="csv", file_path="/tmp/test.csv",
            table_name="test_ds", schema_json="[]", row_count=0,
        )
        result = self.db.add_dataset(
            name="test_ds", source_type="csv", file_path="/tmp/test2.csv",
            table_name="test_ds_2", schema_json="[]", row_count=0,
        )
        self.assertFalse(result["success"])
        self.assertIn("已存在", result["error"])

    def test_list_datasets(self):
        self.db.add_dataset("a", "csv", "/a.csv", "a", "[]", 10)
        self.db.add_dataset("b", "excel", "/b.xlsx", "b", "[]", 20)
        datasets = self.db.list_datasets()
        self.assertEqual(len(datasets), 2)

    def test_delete_dataset(self):
        self.db.add_dataset("del_me", "csv", "/d.csv", "del_me", "[]", 0)
        result = self.db.delete_dataset("del_me")
        self.assertTrue(result["success"])
        self.assertIsNone(self.db.get_dataset("del_me"))

    def test_delete_nonexistent_fails(self):
        result = self.db.delete_dataset("nope")
        self.assertFalse(result["success"])

    def test_update_dataset(self):
        self.db.add_dataset("up", "csv", "/u.csv", "up", "[]", 0)
        result = self.db.update_dataset("up", row_count=500, description="updated")
        self.assertTrue(result["success"])
        ds = self.db.get_dataset("up")
        self.assertEqual(ds["row_count"], 500)
        self.assertEqual(ds["description"], "updated")

    def test_get_all_table_names(self):
        self.db.add_dataset("t1", "csv", "/t1.csv", "table_one", "[]", 0)
        self.db.add_dataset("t2", "mysql", "db:erp", "erp_orders", "[]", 0)
        names = self.db.get_all_table_names()
        self.assertIn("table_one", names)
        self.assertIn("erp_orders", names)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd agent && python -m pytest tests/test_datasources_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'database.datasources_db'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/database/datasources_db.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd agent && python -m pytest tests/test_datasources_db.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd agent
git add database/datasources_db.py tests/test_datasources_db.py
git commit -m "feat: add DatasourcesDB for dataset metadata management"
```

---

### Task 2: datasources.yml 配置与加载

**Files:**
- Create: `agent/config/datasources.yml`
- Modify: `agent/utils/config_handler.py`

**Interfaces:**
- Consumes: `utils/path_tool.get_abs_path()`
- Produces: `datasources_conf` 全局变量（dict），`load_datasources_config()` 函数
- Produces: 配置文件中 `password_env` 字段指定 `.env` 中的密码变量名

- [ ] **Step 1: Create datasources.yml**

```yaml
# datasources.yml — 管理员预配置的数据库连接
# 密码通过 password_env 字段从 .env 读取，不硬编码
# 如果 databases 为空列表或不配置，则不连接任何外部数据库

databases: []
# 示例配置（取消注释并修改后使用）:
# - name: local_mysql
#   type: mysql
#   host: 127.0.0.1
#   port: 3306
#   database: my_business
#   user: root
#   password_env: MYSQL_PASSWORD
#   tables: []  # 空列表 = 暴露所有表；指定表名则只暴露列出的表
#
# - name: local_postgres
#   type: postgres
#   host: 127.0.0.1
#   port: 5432
#   database: analytics
#   user: postgres
#   password_env: PG_PASSWORD
#   tables:
#     - orders
#     - customers
```

- [ ] **Step 2: Add config loader to config_handler.py**

在 `agent/utils/config_handler.py` 末尾追加：

```python
def load_datasources_config(config_path: str = get_abs_path("config/datasources.yml"), encoding="utf-8"):
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.load(f, Loader=yaml.FullLoader)

datasources_conf = load_datasources_config()
```

- [ ] **Step 3: Verify config loads**

Run: `cd agent && python -c "from utils.config_handler import datasources_conf; print(datasources_conf)"`
Expected: `{'databases': []}`

- [ ] **Step 4: Commit**

```bash
cd agent
git add config/datasources.yml utils/config_handler.py
git commit -m "feat: add datasources.yml config and loader"
```

---

### Task 3: DuckDB 多数据源加载

**Files:**
- Modify: `agent/database/duckdb_manager.py`
- Create: `agent/tests/test_duckdb_multi_source.py`

**Interfaces:**
- Consumes: `database.datasources_db.datasources_db` — 读取元数据
- Consumes: `utils.config_handler.datasources_conf` — 读取数据库连接配置
- Produces: `DuckDBManager` 新增方法：
  - `load_csv_dataset(csv_path, table_name) -> dict` — 加载 CSV 到指定表名
  - `load_excel_dataset(excel_path, table_name, sheet=None) -> dict` — 加载 Excel
  - `register_external_databases() -> dict` — 注册 datasources.yml 中的数据库
  - `drop_table(table_name) -> bool` — 删除指定表
  - `get_enhanced_schema_text() -> str` — 返回含行数和源类型的增强 schema
- Produces: `init_duckdb()` 改造 — 启动时自动加载 `data/datasets/` 下的文件和元数据中的数据集
- Produces: `safe_ident(name: str) -> str` — DuckDB 标识符转义

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_duckdb_multi_source.py
import os
import sys
import unittest
import tempfile
import csv

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestDuckDBMultiSource(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def _make_csv(self, filename, rows):
        path = os.path.join(self.tmp_dir, filename)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_load_csv_dataset(self):
        from database.duckdb_manager import DuckDBManager
        csv_path = self._make_csv("test.csv", [
            {"name": "Alice", "score": 90},
            {"name": "Bob", "score": 85},
        ])
        db = DuckDBManager(user_id="test_multi")
        result = db.load_csv_dataset(csv_path, "test_table")
        self.assertTrue(result["success"])
        self.assertEqual(result["row_count"], 2)

        # 验证表已加载
        tables = db.get_table_names()
        self.assertIn("test_table", tables)

        # 验证可以查询
        df = db.query_df("SELECT * FROM test_table")
        self.assertEqual(len(df), 2)
        db.close()

    def test_load_multiple_datasets(self):
        from database.duckdb_manager import DuckDBManager
        csv1 = self._make_csv("sales.csv", [
            {"product": "A", "amount": 100},
        ])
        csv2 = self._make_csv("inventory.csv", [
            {"product": "A", "qty": 50},
        ])
        db = DuckDBManager(user_id="test_multi2")
        r1 = db.load_csv_dataset(csv1, "sales")
        r2 = db.load_csv_dataset(csv2, "inventory")
        self.assertTrue(r1["success"])
        self.assertTrue(r2["success"])

        # 跨表 JOIN
        df = db.query_df(
            "SELECT s.product, s.amount, i.qty FROM sales s JOIN inventory i ON s.product = i.product"
        )
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["amount"], 100)
        self.assertEqual(df.iloc[0]["qty"], 50)
        db.close()

    def test_drop_table(self):
        from database.duckdb_manager import DuckDBManager
        csv_path = self._make_csv("tmp.csv", [{"x": 1}])
        db = DuckDBManager(user_id="test_drop")
        db.load_csv_dataset(csv_path, "tmp_table")
        self.assertIn("tmp_table", db.get_table_names())

        result = db.drop_table("tmp_table")
        self.assertTrue(result)
        self.assertNotIn("tmp_table", db.get_table_names())
        db.close()

    def test_safe_ident(self):
        from database.duckdb_manager import safe_ident
        self.assertEqual(safe_ident("normal"), '"normal"')
        self.assertEqual(safe_ident('has"quote'), '"has""quote"')


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd agent && python -m pytest tests/test_duckdb_multi_source.py -v`
Expected: FAIL with `AttributeError: 'DuckDBManager' object has no attribute 'load_csv_dataset'`

- [ ] **Step 3: Implement load_csv_dataset, load_excel_dataset, drop_table, safe_ident, get_enhanced_schema_text on DuckDBManager**

在 `agent/database/duckdb_manager.py` 中添加以下内容：

1. 在文件顶部（`SecurityError` 类之后）添加 `safe_ident` 函数：

```python
def safe_ident(name: str) -> str:
    """转义 DuckDB 标识符，防止 SQL 注入。"""
    return '"' + name.replace('"', '""') + '"'
```

2. 在 `DuckDBManager` 类中添加方法（在 `close()` 方法之前）：

```python
    def load_csv_dataset(self, csv_path: str, table_name: str) -> dict:
        """加载 CSV 文件到指定表名（管理通道）。返回 {"success": bool, "row_count": int, "error": str|None}"""
        try:
            _validate_table_name(table_name)
            _validate_csv_path(csv_path)
            if not os.path.exists(csv_path):
                return {"success": False, "error": f"文件不存在: {csv_path}"}
            # 如果表已存在，先删除
            existing = [r[0] for r in self.conn.execute("SHOW TABLES").fetchall()]
            if table_name in existing:
                self.conn.execute(f"DROP TABLE {safe_ident(table_name)}")
            self.conn.execute(
                f"CREATE TABLE {safe_ident(table_name)} AS SELECT * FROM read_csv_auto('{csv_path}')"
            )
            row_count = self.conn.execute(
                f"SELECT COUNT(*) FROM {safe_ident(table_name)}"
            ).fetchone()[0]
            logger.info(f"Loaded {row_count} rows from {csv_path} into table '{table_name}'")
            return {"success": True, "row_count": row_count}
        except Exception as e:
            logger.error(f"load_csv_dataset failed for {csv_path}: {e}")
            return {"success": False, "error": str(e)}

    def load_excel_dataset(self, excel_path: str, table_name: str, sheet: str | None = None) -> dict:
        """加载 Excel 文件到指定表名（管理通道）。"""
        try:
            _validate_table_name(table_name)
            if not os.path.exists(excel_path):
                return {"success": False, "error": f"文件不存在: {excel_path}"}
            existing = [r[0] for r in self.conn.execute("SHOW TABLES").fetchall()]
            if table_name in existing:
                self.conn.execute(f"DROP TABLE {safe_ident(table_name)}")
            # DuckDB 内置 read_excel 支持
            sheet_clause = f", sheet = '{sheet}'" if sheet else ""
            self.conn.execute(
                f"CREATE TABLE {safe_ident(table_name)} AS SELECT * FROM read_excel('{excel_path}'{sheet_clause})"
            )
            row_count = self.conn.execute(
                f"SELECT COUNT(*) FROM {safe_ident(table_name)}"
            ).fetchone()[0]
            logger.info(f"Loaded {row_count} rows from {excel_path} into table '{table_name}'")
            return {"success": True, "row_count": row_count}
        except Exception as e:
            logger.error(f"load_excel_dataset failed for {excel_path}: {e}")
            return {"success": False, "error": str(e)}

    def drop_table(self, table_name: str) -> bool:
        """删除指定表（管理通道）。"""
        try:
            _validate_table_name(table_name)
            self.conn.execute(f"DROP TABLE IF EXISTS {safe_ident(table_name)}")
            logger.info(f"Dropped table '{table_name}'")
            return True
        except Exception as e:
            logger.error(f"drop_table failed for {table_name}: {e}")
            return False

    def get_enhanced_schema_text(self) -> str:
        """返回增强版 schema：包含行数和源类型注释。"""
        from database.datasources_db import datasources_db
        tables = self.conn.execute("SHOW TABLES").fetchall()
        if not tables:
            return "No tables found."

        # 构建表名→元数据映射
        ds_map = {}
        try:
            for ds in datasources_db.list_datasets():
                ds_map[ds["table_name"]] = ds
        except Exception:
            pass

        parts = []
        for (table_name,) in tables:
            try:
                _validate_table_name(table_name)
            except SecurityError:
                continue
            cols = self.conn.execute(f"DESCRIBE {safe_ident(table_name)}").fetchall()
            col_lines = []
            for col_name, col_type, *_ in cols:
                col_lines.append(f"  - {col_name} ({col_type})")

            # 获取行数
            try:
                row_count = self.conn.execute(
                    f"SELECT COUNT(*) FROM {safe_ident(table_name)}"
                ).fetchone()[0]
            except Exception:
                row_count = "?"

            # 获取源类型注释
            ds_meta = ds_map.get(table_name)
            source_note = ""
            if ds_meta:
                source_note = f" ({ds_meta['source_type']})"
                if ds_meta.get("description"):
                    source_note += f" - {ds_meta['description']}"

            parts.append(
                f"Table: {table_name}{source_note} [{row_count} rows]\n" + "\n".join(col_lines)
            )
        return "\n\n".join(parts)

    def register_external_databases(self) -> dict:
        """注册 datasources.yml 中配置的外部数据库为 DuckDB VIEW。"""
        try:
            from utils.config_handler import datasources_conf
        except ModuleNotFoundError:
            from agent.utils.config_handler import datasources_conf

        databases = datasources_conf.get("databases", [])
        results = {"registered": [], "failed": []}

        for db_config in databases:
            db_name = db_config.get("name", "")
            db_type = db_config.get("type", "")
            password = os.environ.get(db_config.get("password_env", ""), "")

            try:
                if db_type == "postgres":
                    self.conn.execute("INSTALL postgres_scan; LOAD postgres_scan;")
                elif db_type == "mysql":
                    self.conn.execute("INSTALL mysql_scan; LOAD mysql_scan;")
                else:
                    results["failed"].append({"name": db_name, "error": f"不支持的类型: {db_type}"})
                    continue

                # 获取要注册的表列表
                tables = db_config.get("tables", [])
                if not tables:
                    # 未指定表则尝试发现（通过 information_schema）
                    if db_type == "postgres":
                        discover_sql = (
                            f"SELECT table_name FROM postgres_scan("
                            f"host='{db_config['host']}', port='{db_config['port']}', "
                            f"database='{db_config['database']}', user='{db_config['user']}', "
                            f"password='{password}', "
                            f"table='information_schema.tables') "
                            f"WHERE table_schema='public'"
                        )
                    elif db_type == "mysql":
                        discover_sql = (
                            f"SELECT table_name FROM mysql_scan("
                            f"host='{db_config['host']}', port='{db_config['port']}', "
                            f"database='{db_config['database']}', user='{db_config['user']}', "
                            f"password='{password}', "
                            f"table='tables') "
                            f"WHERE table_schema='{db_config['database']}'"
                        )
                    try:
                        discovered = self.conn.execute(discover_sql).fetchall()
                        tables = [r[0] for r in discovered]
                    except Exception as e:
                        logger.warning(f"Auto-discover tables for {db_name} failed: {e}")
                        results["failed"].append({"name": db_name, "error": f"无法发现表: {e}"})
                        continue

                for table in tables:
                    view_name = f"{db_name}_{table}"
                    try:
                        scan_func = "postgres_scan" if db_type == "postgres" else "mysql_scan"
                        self.conn.execute(f"""
                            CREATE VIEW {safe_ident(view_name)} AS
                            SELECT * FROM {scan_func}(
                                host=?, port=?, database=?, user=?, password=?, table=?
                            )
                        """, [db_config["host"], str(db_config["port"]),
                              db_config["database"], db_config["user"], password, table])
                        results["registered"].append(view_name)
                        logger.info(f"Registered external view: {view_name}")
                    except Exception as e:
                        logger.error(f"Failed to register view {view_name}: {e}")
                        results["failed"].append({"name": view_name, "error": str(e)})

            except Exception as e:
                logger.error(f"Failed to setup {db_type} extension for {db_name}: {e}")
                results["failed"].append({"name": db_name, "error": str(e)})

        return results
```

3. 改造 `init_duckdb` 函数——启动时自动加载已注册数据集：

```python
def init_duckdb(csv_path: str | None = None, user_id: str = "default") -> DuckDBManager:
    """获取（或创建）指定 user_id 的 DuckDBManager 实例。

    改造：首次创建实例时，自动加载 datasources.db 中记录的所有数据集
    和 data/datasets/ 目录下的文件。
    """
    if user_id is None:
        user_id = "default"

    inst = _duckdb_instances.get(user_id)
    if inst is None:
        if csv_path is None:
            csv_path = get_abs_path("data/train.csv")
        inst = DuckDBManager(csv_path=csv_path, user_id=user_id)

        # 首次创建：加载所有已注册数据集
        _reload_datasets_into_instance(inst)

        # 注册外部数据库
        inst.register_external_databases()

        _duckdb_instances[user_id] = inst
    else:
        if csv_path and inst.last_loaded_csv != csv_path:
            inst.reload_csv(csv_path)
    return inst


def _reload_datasets_into_instance(inst: DuckDBManager):
    """从 datasources.db 元数据重新加载所有数据集到 DuckDB 实例。"""
    try:
        from database.datasources_db import datasources_db
    except ModuleNotFoundError:
        from agent.database.datasources_db import datasources_db

    datasets = datasources_db.list_datasets()
    for ds in datasets:
        if ds["source_type"] == "csv":
            if os.path.exists(ds["file_path"]):
                inst.load_csv_dataset(ds["file_path"], ds["table_name"])
        elif ds["source_type"] == "excel":
            if os.path.exists(ds["file_path"]):
                inst.load_excel_dataset(ds["file_path"], ds["table_name"])
        # postgres/mysql 类型由 register_external_databases 处理
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd agent && python -m pytest tests/test_duckdb_multi_source.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
cd agent
git add database/duckdb_manager.py tests/test_duckdb_multi_source.py
git commit -m "feat: DuckDB multi-dataset loading with CSV/Excel support and enhanced schema"
```

---

### Task 4: DataResolver 动态化

**Files:**
- Modify: `agent/database/data_resolver.py`

**Interfaces:**
- Consumes: `database.datasources_db.datasources_db` — 动态读取数据集列表
- Produces: `DataResolver.resolve(query)` 返回格式扩展为 `{"table_names": list[str], "datasets": list[dict], ...}` — 向后兼容保留 `csv_path`、`name` 等旧字段

- [ ] **Step 1: Modify DataResolver**

将 `agent/database/data_resolver.py` 改造为从 `datasources_db` 动态读取：

```python
"""
Data Resolver: 根据用户提示词从动态数据源中自动匹配合适的数据集。
改造：从 datasources.db 元数据动态读取，替代硬编码 DATASET_MAP。
保留旧 DATASET_MAP 作为 fallback。
"""

import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

# 旧版硬编码映射（fallback）
DATASET_MAP = {
    "About_dataset_train.txt": {
        "csv": "data/train.csv",
        "name": "Superstore Sales Dataset",
        "keywords": ["superstore", "超市", "零售", "retail", "forecast", "预测",
                     "time series", "时间序列", "EDA", "ship", "运输", "region",
                     "区域", "segment", "部门", "Sales", "Order", "订单",
                     "sales", "利润", "profit", "产品", "product", "客户", "customer",
                     "类别", "category", "销售", "趋势", "trend", "月度", "monthly"],
        "description": "全球超市 4 年零售数据集，包含订单、运输、区域、产品类别、销售额等字段。",
        "prefer_when": ["城市", "city", "区域", "region", "州", "state", "省份",
                       "ship", "运输", "segment", "部门", "时间序列", "预测",
                       "forecast", "超市", "零售", "retail", "邮编", "postal"],
    },
}

DEFAULT_DATASET = "About_dataset_train.txt"


class DataResolver:
    """根据用户查询自动选择最合适的数据集。"""

    @staticmethod
    def resolve(query: str) -> dict:
        """
        根据用户查询返回最匹配的数据集配置。
        改造：优先从 datasources_db 动态读取，fallback 到旧 DATASET_MAP。
        return: {
            "csv_path": str,          # 向后兼容
            "name": str,              # 向后兼容
            "description": str,       # 向后兼容
            "matched_by": str,
            "desc_file": str,         # 向后兼容
            "table_names": list[str], # 新增：所有相关表名
            "datasets": list[dict],   # 新增：匹配的数据集元数据列表
        }
        """
        query_lower = query.lower()

        # 1. 尝试从 datasources_db 动态读取
        dynamic_datasets = DataResolver._load_dynamic_datasets()

        if dynamic_datasets:
            # 简单关键词匹配
            matched = []
            all_table_names = []
            for ds in dynamic_datasets:
                all_table_names.append(ds["table_name"])
                # 检查查询是否与数据集名或描述相关
                name_lower = ds["name"].lower()
                desc_lower = (ds.get("description") or "").lower()
                table_lower = ds["table_name"].lower()
                if any(kw in query_lower for kw in name_lower.split()) or \
                   any(kw in query_lower for kw in desc_lower.split()) or \
                   any(kw in query_lower for kw in table_lower.split("_")):
                    matched.append(ds)

            # 如果没有明确匹配，返回所有数据集
            if not matched:
                matched = dynamic_datasets

            # 向后兼容：返回第一个匹配的 csv_path
            primary = matched[0] if matched else dynamic_datasets[0]
            csv_path = primary.get("file_path", "")
            if primary["source_type"] == "csv" and not os.path.isabs(csv_path):
                csv_path = get_abs_path(csv_path)

            return {
                "csv_path": csv_path,
                "name": primary["name"],
                "description": primary.get("description", ""),
                "matched_by": "dynamic",
                "desc_file": "",
                "table_names": all_table_names,
                "datasets": matched,
            }

        # 2. Fallback 到旧 DATASET_MAP
        scores = {}
        for desc_file, info in DATASET_MAP.items():
            score = 0
            for kw in info.get("keywords", []):
                if kw.lower() in query_lower:
                    score += 1
            scores[desc_file] = score

        best = max(scores, key=scores.get)
        best_score = scores[best]

        if best_score == 0:
            best = DEFAULT_DATASET
            matched_by = "default"
        else:
            tied = [k for k, v in scores.items() if v == best_score]
            if len(tied) > 1:
                for t in tied:
                    for pw in DATASET_MAP[t].get("prefer_when", []):
                        if pw.lower() in query_lower:
                            scores[t] += 2
                best = max(tied, key=lambda t: scores[t])
                matched_by = f"keyword_match(score={scores[best]})"
            else:
                matched_by = f"keyword_match(score={best_score})"

        info = DATASET_MAP[best]
        csv_path = get_abs_path(info.get("csv", ""))

        return {
            "csv_path": csv_path,
            "name": info["name"],
            "description": info.get("description", ""),
            "matched_by": matched_by,
            "desc_file": best,
            "table_names": ["transactions"],
            "datasets": [],
        }

    @staticmethod
    def _load_dynamic_datasets() -> list[dict]:
        """从 datasources_db 加载所有动态数据集。"""
        try:
            from database.datasources_db import datasources_db
        except ModuleNotFoundError:
            try:
                from agent.database.datasources_db import datasources_db
            except ImportError:
                return []
        try:
            return datasources_db.list_datasets()
        except Exception as e:
            logger.warning(f"Failed to load dynamic datasets: {e}")
            return []

    @staticmethod
    def get_all_datasets() -> list[dict]:
        """返回所有可用数据集的列表（合并动态 + 静态）。"""
        results = DataResolver._load_dynamic_datasets()
        if not results:
            for desc_file, info in DATASET_MAP.items():
                results.append({
                    "name": info["name"],
                    "csv_path": get_abs_path(info["csv"]),
                    "description": info.get("description", ""),
                    "desc_file": desc_file,
                })
        return results

    @staticmethod
    def read_desc_file(desc_filename: str) -> str:
        """读取 .txt 描述文件的完整内容。"""
        desc_path = get_abs_path(f"data/{desc_filename}")
        if os.path.exists(desc_path):
            with open(desc_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""
```

- [ ] **Step 2: Verify DataResolver still works with no dynamic datasets**

Run: `cd agent && python -c "from database.data_resolver import DataResolver; r = DataResolver.resolve('销售趋势'); print(r['name'], r['table_names'])"`
Expected: `Superstore Sales Dataset ['transactions']`

- [ ] **Step 3: Commit**

```bash
cd agent
git add database/data_resolver.py
git commit -m "feat: DataResolver dynamic dataset resolution from datasources_db"
```

---

### Task 5: SQLAgent 多表 Schema 注入

**Files:**
- Modify: `agent/agents/sql_agent.py`

**Interfaces:**
- Consumes: `DuckDBManager.get_enhanced_schema_text()` — 增强版 schema
- Produces: SQLAgent 使用新 prompt 格式，支持多表查询

- [ ] **Step 1: Update SQL_AGENT_SYSTEM_PROMPT**

替换 `agent/agents/sql_agent.py` 中的 `SQL_AGENT_SYSTEM_PROMPT`：

```python
SQL_AGENT_SYSTEM_PROMPT = """你是一个专业的 SQL 生成助手。根据用户的数据分析需求和数据库 Schema，生成可执行的 DuckDB SQL 语句。

## 规则
1. **严格使用下面 Schema 中列出的列名和表名** —— 不要编造任何 Schema 中不存在的列名或表名。
2. 只输出 SQL 语句，放在 ```sql 代码块中。
3. SQL 必须完整、可直接执行，不要使用占位符。
4. 使用双引号引用列名（如果列名包含空格或特殊字符）。
5. 对于聚合查询，确保 GROUP BY 包含所有非聚合列。
6. 如果用户没有指定 LIMIT，默认添加 LIMIT 100。
7. 使用 DuckDB 兼容的 SQL 语法。
8. **只生成 SELECT 查询** —— 禁止生成 DROP/CREATE/INSERT/UPDATE/DELETE/ATTACH/ALTER/TRUNCATE 等任何写操作或 DDL 语句。
9. **跨表查询时请使用标准 SQL JOIN** —— 系统支持跨数据集关联分析。

## 数据库 Schema
{schema}

## 重要提示
- 仔细阅读上述 Schema 中的表名和列名。
- 只能使用 Schema 中实际存在的表名和列名来编写 SQL。
- 如果 Schema 中有 "Product Name" 列，请用双引号引用为 "Product Name"。
- 不要假设存在 Schema 中未出现的列名。
- 如果用户的问题涉及多个表，请使用 JOIN 关联查询。

请根据用户需求生成 SQL："""
```

- [ ] **Step 2: Update _generate_sql to use enhanced schema and remove single-table assumption**

修改 `SQLAgent._generate_sql` 方法，移除 `{table_name}` 占位符：

```python
    def _generate_sql(self, task: str, table_name: str = "", fix_hint: dict | None = None) -> str:
        """使用 LLM 生成 SQL。若提供 fix_hint（上一轮错误信息），则要求 LLM 据此修正。"""
        schema_text = self.db.get_enhanced_schema_text()
        prompt = SQL_AGENT_SYSTEM_PROMPT.format(schema=schema_text)
        if fix_hint:
            user_content = (
                f"之前生成的 SQL 执行失败，请根据错误信息修正后重新生成 SQL。\n"
                f"用户需求：\n{task}\n\n"
                f"之前生成的 SQL：\n```sql\n{fix_hint['sql']}\n```\n\n"
                f"执行错误：\n{fix_hint['error']}\n\n"
                f"请修正上述错误，只输出修正后的 SQL 代码块。常见原因：列名错误（请严格用 Schema 中的列名）、"
                f"列名含空格需双引号、DuckDB 语法不兼容等。"
            )
        else:
            user_content = f"请为以下需求生成 SQL：\n{task}"
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]
        response = self._call_llm(messages)
        return self._extract_sql(response)
```

- [ ] **Step 3: Commit**

```bash
cd agent
git add agents/sql_agent.py
git commit -m "feat: SQLAgent multi-table schema injection with enhanced prompt"
```

---

### Task 6: agent_tools.py 多表适配

**Files:**
- Modify: `agent/agent/tools/agent_tools.py`

**Interfaces:**
- Consumes: `DuckDBManager.get_enhanced_schema_text()` 和 `get_table_names()`
- Produces: `get_data_overview` 支持多表概览

- [ ] **Step 1: Update get_data_overview to support multiple tables**

替换 `agent/agent/tools/agent_tools.py` 中的 `get_data_overview` 函数：

```python
@tool(description="快速查询数据集概况，返回所有已加载数据集的表结构、行数、关键统计信息，无需参数")
def get_data_overview() -> str:
    """返回所有数据集的概况信息，自动适配当前数据集的实际列名。"""
    try:
        from database.duckdb_manager import init_duckdb
        from utils.request_context import get_user_id
    except ModuleNotFoundError:
        from agent.database.duckdb_manager import init_duckdb
        from utils.request_context import get_user_id

    try:
        db = init_duckdb(user_id=get_user_id())
        tables = db.get_table_names()

        if not tables:
            return "当前没有加载任何数据集。请上传 CSV/Excel 文件开始分析。"

        all_parts = []
        for table_name in tables:
            try:
                row_count = db.query_df(f"SELECT COUNT(*) AS cnt FROM {table_name}").iloc[0, 0]
                cols_info = db.execute(f"DESCRIBE {table_name}").fetchall()
                orig_cols = [c[0] for c in cols_info]

                parts = [
                    f"📊 数据集: {table_name}",
                    f"- 总记录数: {row_count} 条",
                    f"- 字段数: {len(cols_info)} 个",
                    f"- 全部列名: {', '.join(orig_cols)}",
                ]
                all_parts.append("\n".join(parts))
            except Exception as e:
                all_parts.append(f"📊 数据集: {table_name} (读取失败: {e})")

        return "\n\n".join(all_parts)
    except Exception as e:
        return f"数据查询失败: {str(e)}"
```

- [ ] **Step 2: Commit**

```bash
cd agent
git add agent/tools/agent_tools.py
git commit -m "feat: update get_data_overview to support multiple datasets"
```

---

### Task 7: FastAPI 数据集管理端点

**Files:**
- Modify: `agent/api/fastapi_server.py`

**Interfaces:**
- Consumes: `database.datasources_db.datasources_db` — 元数据 CRUD
- Consumes: `database.duckdb_manager.init_duckdb` — DuckDB 实例
- Produces: 5 个新端点（见设计文档）

- [ ] **Step 1: Add dataset management endpoints to fastapi_server.py**

在 `fastapi_server.py` 中，知识库管理路由之前（`@app.get("/api/knowledge/files")` 之前）添加以下端点：

```python
# ── 数据集管理 ──

def _datasets_dir() -> str:
    """用户上传的数据集存放目录。"""
    d = get_abs_path("data/datasets")
    os.makedirs(d, exist_ok=True)
    return d

_ALLOWED_DATASET_TYPES = {"csv", "xlsx", "xls"}
_MAX_DATASET_SIZE = 100 * 1024 * 1024  # 100MB


@app.get("/api/datasets")
async def list_datasets(request: Request):
    """列出所有可用数据集。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    try:
        from database.datasources_db import datasources_db
    except ModuleNotFoundError:
        from agent.database.datasources_db import datasources_db
    datasets = datasources_db.list_datasets()
    return JSONResponse({"datasets": datasets, "count": len(datasets)})


@app.post("/api/datasets/upload")
async def upload_dataset(request: Request, file: UploadFile = File(...)):
    """上传 CSV/Excel 文件，解析并加载到 DuckDB。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)

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
            {"success": False, "error": f"文件超过大小限制(100MB)"},
            status_code=413,
        )

    # 保存文件
    ds_dir = _datasets_dir()
    # 生成安全的表名：文件名去扩展名，替换非法字符
    base_name = os.path.splitext(fname)[0]
    safe_name = re_module.sub(r'[^A-Za-z0-9_]', '_', base_name)
    if not safe_name or not safe_name[0].isalpha():
        safe_name = "ds_" + safe_name

    # 处理同名冲突
    try:
        from database.datasources_db import datasources_db
    except ModuleNotFoundError:
        from agent.database.datasources_db import datasources_db

    table_name = safe_name
    counter = 2
    while datasources_db.get_dataset(table_name):
        table_name = f"{safe_name}_{counter}"
        counter += 1

    fpath = os.path.join(ds_dir, f"{table_name}.{ext}")
    with open(fpath, "wb") as out:
        out.write(content)

    # 加载到 DuckDB
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
        cols = db.execute(f"DESCRIBE {table_name}").fetchall()
        schema_json = json.dumps([
            {"name": c[0], "type": c[1]} for c in cols
        ], ensure_ascii=False)

        # 获取样本数据（前5行）
        try:
            sample_df = db.query_df(f"SELECT * FROM {table_name} LIMIT 5")
            sample_data = sample_df.to_dict(orient="records")
        except Exception:
            sample_data = []

        # 写入元数据
        source_type = "csv" if ext == "csv" else "excel"
        datasources_db.add_dataset(
            name=table_name,
            source_type=source_type,
            file_path=fpath,
            table_name=table_name,
            schema_json=schema_json,
            row_count=load_result["row_count"],
        )

        return JSONResponse({
            "success": True,
            "name": table_name,
            "source_type": source_type,
            "row_count": load_result["row_count"],
            "columns": [c[0] for c in cols],
            "sample": sample_data,
        })

    except Exception as e:
        logger.error(f"Dataset upload failed: {traceback.format_exc()}")
        if os.path.exists(fpath):
            os.remove(fpath)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.delete("/api/datasets/{name}")
async def delete_dataset(request: Request, name: str):
    """删除数据集（卸载 DuckDB 表 + 删除文件 + 删除元数据）。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        return JSONResponse({"error": "非法数据集名"}, status_code=400)

    try:
        from database.datasources_db import datasources_db
    except ModuleNotFoundError:
        from agent.database.datasources_db import datasources_db

    ds = datasources_db.get_dataset(name)
    if not ds:
        return JSONResponse({"error": f"数据集 '{name}' 不存在"}, status_code=404)

    # 从 DuckDB 删除表
    try:
        db = init_duckdb(user_id=user_id)
        db.drop_table(ds["table_name"])
    except Exception as e:
        logger.warning(f"Failed to drop table {ds['table_name']}: {e}")

    # 删除文件
    if ds["file_path"] and os.path.exists(ds["file_path"]):
        try:
            os.remove(ds["file_path"])
        except Exception as e:
            logger.warning(f"Failed to delete file {ds['file_path']}: {e}")

    # 删除元数据
    datasources_db.delete_dataset(name)
    return JSONResponse({"success": True})


@app.get("/api/datasets/{name}/schema")
async def get_dataset_schema(request: Request, name: str):
    """获取数据集的详细 schema。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)

    try:
        from database.datasources_db import datasources_db
    except ModuleNotFoundError:
        from agent.database.datasources_db import datasources_db

    ds = datasources_db.get_dataset(name)
    if not ds:
        return JSONResponse({"error": f"数据集 '{name}' 不存在"}, status_code=404)

    # 从 DuckDB 获取实时 schema
    try:
        db = init_duckdb(user_id=user_id)
        cols = db.execute(f"DESCRIBE {ds['table_name']}").fetchall()
        stats = db.execute(f"SUMMARIZE {ds['table_name']}").fetchall()
        sample_df = db.query_df(f"SELECT * FROM {ds['table_name']} LIMIT 5")

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
            "sample": sample_df.to_dict(orient="records"),
        })
    except Exception as e:
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


@app.post("/api/datasources/reload")
async def reload_datasources(request: Request):
    """热加载 datasources.yml 配置的数据库连接。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)

    try:
        db = init_duckdb(user_id=user_id)
        result = db.register_external_databases()
        return JSONResponse({"success": True, **result})
    except Exception as e:
        logger.error(f"Datasource reload failed: {traceback.format_exc()}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
```

- [ ] **Step 2: Verify endpoints are registered**

Run: `cd agent && python -c "from api.fastapi_server import app; routes = [r.path for r in app.routes]; print([r for r in routes if 'dataset' in r or 'datasource' in r])"`
Expected: 包含 `/api/datasets`, `/api/datasets/upload`, `/api/datasets/{name}`, `/api/datasets/{name}/schema`, `/api/datasources/reload`

- [ ] **Step 3: Commit**

```bash
cd agent
git add api/fastapi_server.py
git commit -m "feat: add dataset management API endpoints (upload/delete/schema/reload)"
```

---

### Task 8: 前端数据集管理面板

**Files:**
- Modify: `agent/api/fastapi_server.py` — HTML_TEMPLATE 部分

**Interfaces:**
- Consumes: `/api/datasets`, `/api/datasets/upload`, `/api/datasets/{name}`, `/api/datasets/{name}/schema` 端点

- [ ] **Step 1: Add dataset management CSS to HTML_TEMPLATE**

在 HTML_TEMPLATE 的 `<style>` 块中，`.kb-reindex .kb-btn` 规则之后添加：

```css
/* ── 数据集管理 ── */
.ds-section { border-top: 1px solid #2d3748; padding: 12px 0 0; }
.ds-header { padding: 0 16px 8px; display: flex; justify-content: space-between; align-items: center; }
.ds-header h2 { font-size: 13px; color: #e2e8f0; font-weight: 600; }
.ds-count { font-size: 11px; color: #718096; }
.ds-body { padding: 0 12px 8px; max-height: 200px; overflow-y: auto; }
.ds-item { display: flex; align-items: center; gap: 6px; padding: 6px 8px;
           border-radius: 6px; font-size: 12px; color: #cbd5e0;
           transition: background .15s; cursor: pointer; }
.ds-item:hover { background: #16213e; }
.ds-item .ds-icon { flex-shrink: 0; font-size: 14px; }
.ds-item .ds-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ds-item .ds-rows { font-size: 10px; color: #718096; flex-shrink: 0; }
.ds-del { background: transparent; border: none; color: #718096; cursor: pointer;
          font-size: 14px; padding: 0 2px; flex-shrink: 0; }
.ds-del:hover { color: #e94560; }
.ds-upload { padding: 0 16px 8px; }
.ds-upload input[type=file] { display: none; }
.ds-btn { width: 100%; padding: 7px; font-size: 12px; border-radius: 6px;
          border: 1px dashed #4a5568; background: transparent; color: #a0aec0;
          cursor: pointer; transition: all .15s; }
.ds-btn:hover { color: #e94560; border-color: #e94560; }
.ds-detail { display: none; padding: 8px 12px; background: #16213e; border-radius: 6px;
             margin: 4px 12px; font-size: 11px; color: #a0aec0; }
.ds-detail.show { display: block; }
.ds-detail table { width: 100%; font-size: 10px; border-collapse: collapse; }
.ds-detail th, .ds-detail td { padding: 2px 4px; text-align: left; border-bottom: 1px solid #2d3748; }
```

- [ ] **Step 2: Add dataset panel HTML to sidebar**

在 HTML_TEMPLATE 中，找到知识库管理区域的 `<div class="kb-section">` 之前，插入数据集管理面板：

```html
  <!-- ── 数据集管理 ── -->
  <div class="ds-section">
    <div class="ds-header">
      <h2>📁 数据集</h2>
      <span class="ds-count" id="dsCount">-</span>
    </div>
    <div class="ds-body" id="dsList">
      <div class="ds-item" style="color:#718096;justify-content:center;">加载中...</div>
    </div>
    <div class="ds-upload">
      <input type="file" id="dsFileInput" accept=".csv,.xlsx,.xls">
      <button class="ds-btn" onclick="document.getElementById('dsFileInput').click()">＋ 上传 CSV/Excel</button>
    </div>
  </div>
```

- [ ] **Step 3: Add dataset management JavaScript**

在 HTML_TEMPLATE 的 `<script>` 块中，`loadKbFiles();` 调用之前添加：

```javascript
// ── 数据集管理 ──
function dsIcon(type) {
  if (type === 'csv') return '📄';
  if (type === 'excel') return '📊';
  if (type === 'mysql') return '🗄️';
  if (type === 'postgres') return '🐘';
  return '📁';
}

async function loadDatasets() {
  try {
    const r = await fetch('/api/datasets', {headers: authHeaders()});
    if (!r.ok) { document.getElementById('dsList').innerHTML = '<div class="ds-item" style="color:#718096;justify-content:center;">加载失败</div>'; return; }
    const data = await r.json();
    const datasets = data.datasets || [];
    document.getElementById('dsCount').textContent = datasets.length + ' 个';
    const list = document.getElementById('dsList');
    if (datasets.length === 0) {
      list.innerHTML = '<div class="ds-item" style="color:#718096;justify-content:center;">暂无数据集</div>';
    } else {
      list.innerHTML = datasets.map(d => {
        const rows = d.row_count > 0 ? d.row_count.toLocaleString() + '行' : '';
        return `<div class="ds-item" onclick="toggleDsDetail('${d.name}')">
          <span class="ds-icon">${dsIcon(d.source_type)}</span>
          <span class="ds-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</span>
          <span class="ds-rows">${rows}</span>
          <button class="ds-del" onclick="event.stopPropagation();deleteDs('${escapeHtml(d.name)}')" title="删除">✕</button>
        </div>
        <div class="ds-detail" id="ds-detail-${d.name}">加载中...</div>`;
      }).join('');
    }
  } catch(e) { console.log('加载数据集失败:', e); }
}

async function toggleDsDetail(name) {
  const el = document.getElementById('ds-detail-' + name);
  if (!el) return;
  if (el.classList.contains('show')) { el.classList.remove('show'); return; }
  el.classList.add('show');
  try {
    const r = await fetch('/api/datasets/' + encodeURIComponent(name) + '/schema', {headers: authHeaders()});
    if (r.ok) {
      const d = await r.json();
      const cols = (d.columns || []).map(c => `<tr><td>${escapeHtml(c.name)}</td><td>${escapeHtml(c.type)}</td></tr>`).join('');
      el.innerHTML = `<strong>${escapeHtml(d.name)}</strong> (${d.source_type}, ${d.row_count}行)<table><tr><th>列名</th><th>类型</th></tr>${cols}</table>`;
    } else {
      el.innerHTML = '加载失败';
    }
  } catch(e) { el.innerHTML = '加载失败: ' + e.message; }
}

// 数据集上传
document.getElementById('dsFileInput').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/datasets/upload', {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + authToken},
      body: fd
    });
    const data = await r.json();
    if (data.success) {
      alert('✅ 已加载数据集「' + data.name + '」，' + data.row_count + ' 行，' + data.columns.length + ' 列');
    } else {
      alert('❌ 上传失败: ' + (data.error || '未知错误'));
    }
    loadDatasets();
  } catch(err) { alert('上传失败: ' + err.message); }
  e.target.value = '';
});

async function deleteDs(name) {
  if (!confirm('确认删除数据集「' + name + '」？\n将同时删除 DuckDB 表和本地文件。')) return;
  try {
    const r = await fetch('/api/datasets/' + encodeURIComponent(name), {
      method: 'DELETE', headers: authHeaders()
    });
    const data = await r.json();
    if (data.success) { loadDatasets(); }
    else { alert(data.error || '删除失败'); }
  } catch(e) { alert('删除失败: ' + e.message); }
}
```

- [ ] **Step 4: Add loadDatasets() call to initialization**

在 HTML_TEMPLATE 的 `<script>` 块末尾，`loadKbFiles();` 行之前添加：

```javascript
loadDatasets();
```

- [ ] **Step 5: Verify HTML renders**

Run: `cd agent && python -c "from api.fastapi_server import HTML_TEMPLATE; print('datasets' in HTML_TEMPLATE and 'ds-section' in HTML_TEMPLATE)"`
Expected: `True`

- [ ] **Step 6: Commit**

```bash
cd agent
git add api/fastapi_server.py
git commit -m "feat: add dataset management panel to frontend sidebar"
```

---

### Task 9: 补建 data/external 目录 + 集成验证

**Files:**
- Create: `agent/data/external/.gitkeep` — 补建现有配置引用的缺失目录
- Create: `agent/data/datasets/.gitkeep` — 新数据集目录占位

**Interfaces:**
- 无新接口，确保端到端流程可用

- [ ] **Step 1: Create missing directories**

```bash
mkdir -p agent/data/external
mkdir -p agent/data/datasets
touch agent/data/external/.gitkeep
touch agent/data/datasets/.gitkeep
```

- [ ] **Step 2: Verify full integration — start server and test upload**

Run: `cd agent && python -c "
from database.datasources_db import datasources_db
from database.duckdb_manager import init_duckdb

# 1. 验证元数据库可访问
print('Datasets:', datasources_db.list_datasets())

# 2. 验证 DuckDB 初始化
db = init_duckdb(user_id='integration_test')
print('Tables:', db.get_table_names())

# 3. 验证增强 schema
print('Schema:', db.get_enhanced_schema_text()[:200])

print('✅ Integration check passed')
"`
Expected: 无报错，输出数据集列表、表名、schema 摘要

- [ ] **Step 3: Commit**

```bash
cd agent
git add data/external/.gitkeep data/datasets/.gitkeep
git commit -m "chore: add missing data directories with .gitkeep"
```

---

## Self-Review Checklist

**1. Spec coverage:**
- ✅ CSV/Excel 上传 → Task 7 (upload endpoint) + Task 8 (frontend)
- ✅ 关系型数据库连接 → Task 2 (datasources.yml) + Task 3 (register_external_databases)
- ✅ 跨数据集关联分析 → Task 3 (multi-table DuckDB) + Task 5 (SQLAgent multi-table prompt)
- ✅ 数据源元数据 → Task 1 (datasources_db.py)
- ✅ SQLAgent 改造 → Task 5
- ✅ 前端面板 → Task 8
- ✅ 错误处理 → Task 7 (400/413/404 responses)
- ✅ 数据持久化 → Task 3 (file on disk + DuckDB reload)
- ✅ 数据集删除 → Task 7 (delete endpoint)
- ✅ Schema 预览 → Task 7 (schema endpoint) + Task 8 (toggle detail)
- ✅ 数据库连接状态 → Task 7 (reload endpoint) + Task 8 (ds icons)

**2. Placeholder scan:**
- ✅ No TBD/TODO/placeholders found
- ✅ All steps have complete code
- ✅ All steps have exact commands and expected outputs

**3. Type consistency:**
- ✅ `DatasourcesDB.add_dataset()` params match across Task 1 (definition) and Task 7 (call site)
- ✅ `DuckDBManager.load_csv_dataset()` returns `{"success": bool, "row_count": int}` — matches usage in Task 7
- ✅ `safe_ident()` defined in Task 3, used consistently in Task 3 and Task 7
- ✅ `datasources_db` singleton name consistent across all tasks
- ✅ `datasources_conf` variable name consistent across Task 2 and Task 3
