"""POST /api/datasources/reload 与 POST /api/knowledge/reindex 端点测试。

- reload：仅 database.duckdb_manager.init_duckdb 一个桩（路由内函数级 import）
- reindex：仅 api.deps._get_vector_store 一个桩（conftest.swap_srv_seam）
"""
import database.duckdb_manager as duck_mod
from tests.test_datasets_api import _FakeDuckDB


# ── /api/datasources/reload ──

def test_reload_success(client, auth, monkeypatch):
    duck = _FakeDuckDB()
    monkeypatch.setattr(duck_mod, "init_duckdb", lambda user_id=None: duck)

    r = client.post("/api/datasources/reload", headers=auth["headers"])

    assert r.status_code == 200
    assert r.json() == {
        "success": True, "registered": ["mysql_main"], "failed": [],
    }
    assert duck.register_calls == 1


def test_reload_failure_returns_500(client, auth, monkeypatch):
    duck = _FakeDuckDB(register_exc=RuntimeError("yml 不可达"))
    monkeypatch.setattr(duck_mod, "init_duckdb", lambda user_id=None: duck)

    r = client.post("/api/datasources/reload", headers=auth["headers"])

    assert r.status_code == 500
    assert r.json()["success"] is False
    assert "yml 不可达" in r.json()["error"]


def test_reload_requires_auth(client):
    assert client.post("/api/datasources/reload").status_code == 401


# ── /api/knowledge/reindex ──

class _FakeVectorStore:
    def __init__(self, result=None, exc=None):
        self.result = result or {"reloaded_files": 2, "total_files": 3,
                                 "stats": {"chunks": 9}}
        self.exc = exc
        self.calls: list[str] = []

    def reindex_all(self, user_id):
        self.calls.append(user_id)
        if self.exc:
            raise self.exc
        return dict(self.result)


def test_reindex_requires_confirm(client, auth, swap_srv_seam):
    r = client.post("/api/knowledge/reindex", json={}, headers=auth["headers"])
    assert r.status_code == 400
    assert "confirm" in r.json()["error"]


def test_reindex_success_passes_caller_user_id(client, auth, swap_srv_seam):
    vs = _FakeVectorStore()
    swap_srv_seam("_get_vector_store", lambda: vs)

    r = client.post("/api/knowledge/reindex",
                    json={"confirm": True}, headers=auth["headers"])

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["reloaded_files"] == 2
    assert body["total_files"] == 3
    # 重建以调用者 user_id 隔离执行
    assert vs.calls == [auth["user_id"]]


def test_reindex_failure_returns_500(client, auth, swap_srv_seam):
    vs = _FakeVectorStore(exc=RuntimeError("chroma busy"))
    swap_srv_seam("_get_vector_store", lambda: vs)

    r = client.post("/api/knowledge/reindex",
                    json={"confirm": True}, headers=auth["headers"])

    assert r.status_code == 500
    assert "chroma busy" in r.json()["error"]
