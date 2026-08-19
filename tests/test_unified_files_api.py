import os
import time

from fastapi.testclient import TestClient

import api.fastapi_server as srv
import database.user_db as user_db_mod


def _make_authed_user(prefix: str = "files"):
    """注册一个真实用户并登录，返回 Bearer 头。"""
    account = f"{prefix}_{int(time.time() * 1000)}_{os.getpid()}"
    pwd = "Test1234!"
    reg = user_db_mod.user_db.register(account, pwd)
    assert reg.get("success"), reg
    login_res = user_db_mod.user_db.login(account, pwd)
    assert login_res.get("success"), login_res
    return {"Authorization": f"Bearer {login_res['token']}"}


def test_files_returns_list():
    headers = _make_authed_user()
    client = TestClient(srv.app)
    r = client.get("/api/files", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["files"], list)
    # 每项含必要字段
    for f in body["files"]:
        assert "name" in f and "type" in f and "status" in f
        assert f["type"] in ("text", "table")


def test_files_anonymous_rejected():
    client = TestClient(srv.app)
    r = client.get("/api/files")
    assert r.status_code == 401
