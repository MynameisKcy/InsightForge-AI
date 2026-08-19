"""POST /api/report/export 端点测试。"""
import os
import time

from fastapi.testclient import TestClient

import api.fastapi_server as srv
import database.user_db as user_db_mod


def _make_authed_user(prefix: str = "exp"):
    """注册一个真实用户并登录，返回 Bearer 头（沿用项目既有鉴权测试模式）。"""
    account = f"{prefix}_{int(time.time() * 1000)}_{os.getpid()}"
    pwd = "Test1234!"
    reg = user_db_mod.user_db.register(account, pwd)
    assert reg.get("success"), reg
    login_res = user_db_mod.user_db.login(account, pwd)
    assert login_res.get("success"), login_res
    return {"Authorization": f"Bearer {login_res['token']}"}


client = TestClient(srv.app)


def test_export_docx_success():
    headers = _make_authed_user()
    resp = client.post("/api/report/export", json={
        "markdown": "# 报告\n\n正文",
        "title": "测试报告",
        "format": "docx",
    }, headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd")
    assert len(resp.content) > 0


def test_export_empty_markdown_returns_400():
    headers = _make_authed_user()
    resp = client.post("/api/report/export", json={
        "markdown": "",
        "title": "t",
        "format": "md",
    }, headers=headers)
    assert resp.status_code == 400


def test_export_bad_format_returns_400():
    headers = _make_authed_user()
    resp = client.post("/api/report/export", json={
        "markdown": "x",
        "title": "t",
        "format": "xlsx",
    }, headers=headers)
    assert resp.status_code == 400
