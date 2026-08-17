"""tests/ 共享 fixtures：TestClient、真实认证用户、srv 模块级工厂换桩。

沿用既有端点测试模式（test_session_routes / test_settings_api）：
- 真实注册+登录（真实 users.db，唯一账号防跨运行冲突），Bearer 头认证；
- 手工替换 api.fastapi_server 模块级工厂函数、用毕恢复（不用 dependency_overrides）。
"""
import secrets
import time

import pytest
from fastapi.testclient import TestClient

import api.fastapi_server as srv
import database.user_db as user_db_mod


@pytest.fixture
def client() -> TestClient:
    return TestClient(srv.app)


@pytest.fixture
def auth() -> dict:
    """真实注册+登录的用户：{"user_id", "account", "headers"}。"""
    account = f"t_{secrets.token_hex(4)}_{int(time.time() * 1000)}"
    reg = user_db_mod.user_db.register(account, "Test1234!")
    assert reg.get("success"), reg
    login = user_db_mod.user_db.login(account, "Test1234!")
    assert login.get("success"), login
    return {
        "user_id": login["user_id"],
        "account": account,
        "headers": {"Authorization": f"Bearer {login['token']}"},
    }


@pytest.fixture
def auth_headers(auth) -> dict:
    return auth["headers"]


@pytest.fixture
def swap_srv_seam():
    """临时替换 srv 模块级工厂（_get_react_agent 等），测试结束逆序恢复。"""
    replaced: list = []

    def _swap(name: str, fake) -> None:
        replaced.append((name, getattr(srv, name)))
        setattr(srv, name, fake)

    yield _swap

    for name, orig in reversed(replaced):
        setattr(srv, name, orig)
