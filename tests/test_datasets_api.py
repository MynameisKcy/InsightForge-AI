"""/api/datasets* 端点测试：上传 / 列表 / 删除 / schema。

隔离方式（对齐既有测试习惯）：
- _datasets_dir → tmp_path（上传与删除的文件落盘位置）
- datasources_db 模块单例 → tmp SQLite（路由内函数级 import，打源模块即可）
- duckdb_manager.init_duckdb → fake（load/DESCRIBE/SUMMARIZE/query_df/drop_table 桩）
"""
import glob
import os

import pandas as pd
import pytest

import api.fastapi_server as srv
import database.datasources_db as ds_mod
import database.duckdb_manager as duck_mod
from database.datasources_db import DatasourcesDB


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeDuckDB:
    """DuckDBManager 查询面桩：upload/schema/delete/reload 路由用到的方法。"""

    def __init__(self, load_result=None, describe_exc=None,
                 register_exc=None):
        self.load_result = load_result or {"success": True, "row_count": 2}
        self.describe_exc = describe_exc
        self.register_exc = register_exc
        self.cols = [("city", "VARCHAR"), ("revenue", "DOUBLE")]
        self.stats = [
            ("city", "VARCHAR", "济南", "青岛", None, None, 2, 0),
            ("revenue", "DOUBLE", "100.0", "200.0", "150.0", "50.0", 2, 0),
        ]
        self.sample = [{"city": "济南", "revenue": 100.0},
                       {"city": "青岛", "revenue": 200.0}]
        self.dropped: list[str] = []
        self.register_calls = 0

    def load_csv_dataset(self, fpath, table):
        return dict(self.load_result)

    def load_excel_dataset(self, fpath, table):
        return dict(self.load_result)

    def execute(self, sql):
        if self.describe_exc:
            raise self.describe_exc
        if sql.startswith("DESCRIBE"):
            return _FakeCursor(self.cols)
        if sql.startswith("SUMMARIZE"):
            return _FakeCursor(self.stats)
        raise AssertionError(f"unexpected sql: {sql}")

    def query_df(self, sql):
        return pd.DataFrame(self.sample)

    def drop_table(self, table):
        self.dropped.append(table)

    def register_external_databases(self):
        self.register_calls += 1
        if self.register_exc:
            raise self.register_exc
        return {"registered": ["mysql_main"], "failed": []}


@pytest.fixture
def ds_env(tmp_path, monkeypatch):
    """数据集端点隔离环境：{'dir', 'meta', 'duck'}。"""
    monkeypatch.setattr(srv, "_datasets_dir", lambda: str(tmp_path))
    meta = DatasourcesDB(db_path=str(tmp_path / "ds_meta.db"))
    monkeypatch.setattr(ds_mod, "datasources_db", meta)
    duck = _FakeDuckDB()
    monkeypatch.setattr(duck_mod, "init_duckdb", lambda user_id=None: duck)
    return {"dir": tmp_path, "meta": meta, "duck": duck}


def _seed(meta: DatasourcesDB, name: str, owner: str, file_path: str = "",
          schema_json: str = "[]", row_count: int = 5) -> None:
    res = meta.add_dataset(
        name=name, source_type="csv", file_path=file_path, table_name=name,
        schema_json=schema_json, row_count=row_count, owner_user_id=owner,
        display_name=name,
    )
    assert res.get("success"), res


# ── 上传 ──

def test_upload_csv_success(client, auth, ds_env):
    r = client.post(
        "/api/datasets/upload",
        files={"file": ("sales.csv", b"city,revenue\n100,1\n200,2\n", "text/csv")},
        headers=auth["headers"],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["name"] == "sales"
    assert body["row_count"] == 2
    assert body["columns"] == ["city", "revenue"]
    assert body["sample"][0]["city"] == "济南"
    # 文件落盘 + 元数据带 owner 隔离
    assert os.path.isfile(str(ds_env["dir"] / "sales.csv"))
    row = ds_env["meta"].get_dataset("sales", owner_user_id=auth["user_id"])
    assert row is not None and row["owner_user_id"] == auth["user_id"]


def test_upload_chinese_filename_preserves_display_name(client, auth, ds_env):
    r = client.post(
        "/api/datasets/upload",
        files={"file": ("山东数据.csv", b"a,b\n1,2\n", "text/csv")},
        headers=auth["headers"],
    )
    assert r.status_code == 200
    body = r.json()
    # 中文文件名 → 安全 ASCII 表名，display_name 保留中文原名
    assert body["name"] == "ds_upload"
    assert body["display_name"] == "山东数据"


def test_upload_rejects_bad_extension(client, auth, ds_env):
    r = client.post(
        "/api/datasets/upload",
        files={"file": ("data.txt", b"hello", "text/plain")},
        headers=auth["headers"],
    )
    assert r.status_code == 400
    assert "不支持的文件类型" in r.json()["error"]
    assert glob.glob(str(ds_env["dir"] / "*.txt")) == []


def test_upload_load_failure_removes_file(client, auth, ds_env):
    ds_env["duck"].load_result = {"success": False, "error": "bad csv"}
    r = client.post(
        "/api/datasets/upload",
        files={"file": ("broken.csv", b"not,a,csv", "text/csv")},
        headers=auth["headers"],
    )
    assert r.status_code == 400
    assert r.json()["error"] == "bad csv"
    # 加载失败 → 文件被清理，不残留
    assert glob.glob(str(ds_env["dir"] / "*.csv")) == []


def test_upload_exception_returns_500_and_removes_file(client, auth, ds_env):
    ds_env["duck"].describe_exc = RuntimeError("duck exploded")
    r = client.post(
        "/api/datasets/upload",
        files={"file": ("boom.csv", b"a,b\n1,2\n", "text/csv")},
        headers=auth["headers"],
    )
    assert r.status_code == 500
    assert glob.glob(str(ds_env["dir"] / "*.csv")) == []


# ── 列表 ──

def test_list_datasets_filters_by_owner(client, auth, ds_env):
    _seed(ds_env["meta"], "mine", owner=auth["user_id"])
    _seed(ds_env["meta"], "theirs", owner="someone_else")

    r = client.get("/api/datasets", headers=auth["headers"])

    assert r.status_code == 200
    names = [d["name"] for d in r.json()["datasets"]]
    assert names == ["mine"]
    assert r.json()["count"] == 1


# ── 删除 ──

def test_delete_dataset_rejects_path_traversal(client, auth, ds_env):
    # "/" 会被 httpx 规范化掉根本到不了路由，用反斜杠触发路由内的非法名校验
    r = client.delete("/api/datasets/a%5Cb", headers=auth["headers"])
    assert r.status_code == 400
    assert r.json()["error"] == "非法数据集名"


def test_delete_dataset_of_other_user_returns_404(client, auth, ds_env):
    _seed(ds_env["meta"], "theirs", owner="someone_else")
    r = client.delete("/api/datasets/theirs", headers=auth["headers"])
    assert r.status_code == 404
    # 未越权删除
    assert ds_env["meta"].get_dataset("theirs") is not None


def test_delete_dataset_success(client, auth, ds_env):
    fpath = ds_env["dir"] / "mydata.csv"
    fpath.write_text("a,b\n1,2", encoding="utf-8")
    _seed(ds_env["meta"], "mydata", owner=auth["user_id"],
          file_path=str(fpath))

    r = client.delete("/api/datasets/mydata", headers=auth["headers"])

    assert r.status_code == 200
    assert r.json() == {"success": True}
    assert ds_env["duck"].dropped == ["mydata"]
    assert not fpath.exists()
    assert ds_env["meta"].get_dataset("mydata") is None


# ── Schema ──

def test_schema_returns_live_stats(client, auth, ds_env):
    _seed(ds_env["meta"], "sales", owner=auth["user_id"])
    r = client.get("/api/datasets/sales/schema", headers=auth["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == [
        {"name": "city", "type": "VARCHAR"},
        {"name": "revenue", "type": "DOUBLE"},
    ]
    assert body["statistics"][0]["column"] == "city"
    assert body["statistics"][0]["min"] == "济南"
    assert body["sample"][0]["city"] == "济南"


def test_schema_missing_returns_404(client, auth, ds_env):
    r = client.get("/api/datasets/ghost/schema", headers=auth["headers"])
    assert r.status_code == 404


def test_schema_falls_back_to_cached_metadata(client, auth, ds_env):
    # DuckDB 异常 → 回退元数据 schema_json
    ds_env["duck"].describe_exc = RuntimeError("table not loaded")
    _seed(ds_env["meta"], "cached", owner=auth["user_id"],
          schema_json='[{"name": "city", "type": "VARCHAR"}]')
    r = client.get("/api/datasets/cached/schema", headers=auth["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == [{"name": "city", "type": "VARCHAR"}]
    assert "缓存 schema" in body["note"]


def test_datasets_require_auth(client):
    assert client.get("/api/datasets").status_code == 401
