import os, sys, importlib
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient
import api.fastapi_server as srv
import database.user_settings_db as usd_mod

async def _patched_user_id(request):
    return "test_user"

def _fresh_settings(tmp_path):
    # reload 会重置模块级 DB_PATH 到真实路径，故 reload 后再覆盖 + 重建单例
    importlib.reload(usd_mod)
    usd_mod.DB_PATH = str(tmp_path / "u.db")
    usd_mod._ensure_db()
    usd_mod._init_db()
    usd_mod.user_settings_db = usd_mod.UserSettingsDB()
    # 重新绑定到 server 模块已导入的引用
    srv.user_settings_db = usd_mod.user_settings_db

def test_status_unconfigured(tmp_path, monkeypatch):
    _fresh_settings(tmp_path)
    monkeypatch.setattr(srv, "_get_user_id", _patched_user_id)
    client = TestClient(srv.app)
    r = client.get("/api/settings/status")
    assert r.status_code == 200
    assert r.json()["configured"] is False

def test_save_then_masked_get(tmp_path, monkeypatch):
    _fresh_settings(tmp_path)
    monkeypatch.setattr(srv, "_get_user_id", _patched_user_id)
    client = TestClient(srv.app)
    r = client.post("/api/settings", json={"llm_api_key": "sk-secretkey123456", "llm_model_name": "qwen-max"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.get("/api/settings")
    body = r.json()
    assert body["configured"] is True
    assert "****" in body["settings"]["llm_api_key"]
    assert "secretkey123456" not in body["settings"]["llm_api_key"]
    assert body["settings"]["llm_model_name"] == "qwen-max"

def test_anonymous_rejected(tmp_path, monkeypatch):
    _fresh_settings(tmp_path)
    async def _anon(request):
        return "anonymous"
    monkeypatch.setattr(srv, "_get_user_id", _anon)
    client = TestClient(srv.app)
    r = client.get("/api/settings")
    assert r.status_code == 401

def test_masked_key_not_overwritten(tmp_path, monkeypatch):
    """前端回传掩码值时不应覆盖已存的明文 key。"""
    _fresh_settings(tmp_path)
    monkeypatch.setattr(srv, "_get_user_id", _patched_user_id)
    client = TestClient(srv.app)
    client.post("/api/settings", json={"llm_api_key": "sk-secretkey123456", "llm_model_name": "qwen-max"})
    # 回传掩码值 + 改模型名
    client.post("/api/settings", json={"llm_api_key": "sk-****456", "llm_model_name": "qwen-plus"})
    # 取明文校验 key 未变
    data = srv.user_settings_db.get("test_user")
    assert data["llm_api_key"] == "sk-secretkey123456"
    assert data["llm_model_name"] == "qwen-plus"
