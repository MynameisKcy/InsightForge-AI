import os, sys
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient
import api.fastapi_server as srv

async def _patched_user_id(request):
    return "test_user"

def test_files_returns_list(monkeypatch):
    monkeypatch.setattr(srv, "_get_user_id", _patched_user_id)
    client = TestClient(srv.app)
    r = client.get("/api/files")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["files"], list)
    # 每项含必要字段
    for f in body["files"]:
        assert "name" in f and "type" in f and "status" in f
        assert f["type"] in ("text", "table")

def test_files_anonymous_rejected(monkeypatch):
    async def _anon(request):
        return "anonymous"
    monkeypatch.setattr(srv, "_get_user_id", _anon)
    client = TestClient(srv.app)
    r = client.get("/api/files")
    assert r.status_code == 401
