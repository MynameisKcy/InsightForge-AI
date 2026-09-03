import importlib
import os
import time

from fastapi.testclient import TestClient

import api.fastapi_server as srv
import database.user_db as user_db_mod
import database.user_settings_db as usd_mod


def _fresh_settings(tmp_path):
    # reload 会重置模块级 DB_PATH 到真实路径，故 reload 后再覆盖 + 重建单例
    # （settings 路由经属主模块 usd.user_settings_db 动态解析，重绑模块属性即生效）
    importlib.reload(usd_mod)
    usd_mod.DB_PATH = str(tmp_path / "u.db")
    usd_mod._ensure_db()
    usd_mod._init_db()
    usd_mod.user_settings_db = usd_mod.UserSettingsDB()


def _make_authed_user(prefix: str = "set"):
    """注册一个真实用户并登录，返回 (user_id, Bearer 头)。"""
    account = f"{prefix}_{int(time.time() * 1000)}_{os.getpid()}"
    pwd = "Test1234!"
    reg = user_db_mod.user_db.register(account, pwd)
    assert reg.get("success"), reg
    login_res = user_db_mod.user_db.login(account, pwd)
    assert login_res.get("success"), login_res
    return reg["user_id"], {"Authorization": f"Bearer {login_res['token']}"}


def test_status_unconfigured(tmp_path):
    _fresh_settings(tmp_path)
    _user_id, headers = _make_authed_user("status")
    client = TestClient(srv.app)
    r = client.get("/api/settings/status", headers=headers)
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_save_then_masked_get(tmp_path):
    _fresh_settings(tmp_path)
    user_id, headers = _make_authed_user("save")
    client = TestClient(srv.app)
    r = client.post("/api/settings", json={"llm_api_key": "sk-secretkey123456", "llm_model_name": "qwen-max"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.get("/api/settings", headers=headers)
    body = r.json()
    assert body["configured"] is True
    assert "****" in body["settings"]["llm_api_key"]
    assert "secretkey123456" not in body["settings"]["llm_api_key"]
    assert body["settings"]["llm_model_name"] == "qwen-max"


def test_anonymous_rejected(tmp_path):
    _fresh_settings(tmp_path)
    client = TestClient(srv.app)
    r = client.get("/api/settings")
    assert r.status_code == 401


def test_masked_key_not_overwritten(tmp_path):
    """前端回传掩码值时不应覆盖已存的明文 key。"""
    _fresh_settings(tmp_path)
    user_id, headers = _make_authed_user("mask")
    client = TestClient(srv.app)
    client.post("/api/settings", json={"llm_api_key": "sk-secretkey123456", "llm_model_name": "qwen-max"}, headers=headers)
    # 回传掩码值 + 改模型名
    client.post("/api/settings", json={"llm_api_key": "sk-****456", "llm_model_name": "qwen-plus"}, headers=headers)
    # 取明文校验 key 未变
    data = usd_mod.user_settings_db.get(user_id)
    assert data["llm_api_key"] == "sk-secretkey123456"
    assert data["llm_model_name"] == "qwen-plus"


def test_save_enable_thinking_normalized(tmp_path):
    """思考开关路由层宽松布尔归一：字符串 "true"/"1" -> True，"0" -> False。"""
    _fresh_settings(tmp_path)
    _user_id, headers = _make_authed_user("think")
    client = TestClient(srv.app)
    r = client.post("/api/settings", json={"llm_enable_thinking": "true"}, headers=headers)
    assert r.status_code == 200
    r = client.get("/api/settings", headers=headers)
    assert r.json()["settings"]["llm_enable_thinking"] is True
    r = client.post("/api/settings", json={"llm_enable_thinking": "0"}, headers=headers)
    assert r.status_code == 200
    r = client.get("/api/settings", headers=headers)
    assert r.json()["settings"]["llm_enable_thinking"] is False


def test_settings_save_hot_reloads_chat_model(tmp_path, monkeypatch):
    """热重载回归（存量 bug：保存设置后对话仍用旧模型，重启才生效）。

    真实走 POST /api/settings（reload_model_config + _invalidate_user_agents），
    桩掉 create_agent 捕获每次构建 ReactAgent 时拿到的模型实例：
    保存新模型后重建的 agent 必须拿到【不同的实例】且解析到新模型名。
    """
    from api import deps
    from model.factory import get_chat_model_name

    _fresh_settings(tmp_path)
    user_id, headers = _make_authed_user("hot")
    client = TestClient(srv.app)

    captured = []

    class _StubAgent:
        def stream(self, *a, **k):
            yield {}

    def fake_create_agent(model, **kw):
        captured.append(model)
        return _StubAgent()

    monkeypatch.setattr("agent.react_agent.create_agent", fake_create_agent)

    # 首次配置 model-A 并构建 agent
    r = client.post("/api/settings", json={"llm_model_name": "model-A"}, headers=headers)
    assert r.status_code == 200
    a1 = deps._get_react_agent(user_id)
    m1 = captured[-1]

    # 改配置为 model-B 再保存 → 热重载链全真
    r = client.post("/api/settings", json={"llm_model_name": "model-B"}, headers=headers)
    assert r.status_code == 200

    a2 = deps._get_react_agent(user_id)   # 旧实例应已被丢弃，此处重建
    m2 = captured[-1]
    assert a2 is not a1, "ReactAgent 实例未被丢弃重建——_invalidate_user_agents 未生效"
    assert m2 is not m1, "重建 agent 拿到了同一个模型实例——模型缓存未失效（热重载 bug 回归）"
    assert get_chat_model_name(user_id) == "model-B"
