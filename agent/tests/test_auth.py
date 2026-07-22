import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time as _time
import secrets as _secrets

import database.user_db as user_db_mod


def _uniq_account(label: str = "u") -> str:
    """每次生成唯一账号，避免跨测试运行的用户名冲突/密码残留。"""
    return f"{label}_{_secrets.token_hex(4)}_{int(_time.time()) % 100000}"


def _register(name: str = "authtest"):
    user_db_mod.user_db.register(name, "Test1234!")


def _login(name: str = "authtest"):
    return user_db_mod.user_db.login(name, "Test1234!")


def _make_request(token: str | None):
    class H:
        def __init__(self, t): self._h = {"authorization": f"Bearer {t}"} if t else {}
        def get(self, k, d=""): return self._h.get(k.lower(), d)
    class R:
        def __init__(self, t): self.headers = H(t)
    return R(token)


def test_require_auth_rejects_missing_token():
    from api.auth import require_auth
    import pytest
    with pytest.raises(Exception) as e:
        require_auth(_make_request(None))
    assert "401" in str(e.value) or e.value.status_code == 401


def test_require_auth_accepts_valid_token():
    from api.auth import require_auth, extract_token, validate_token_cached
    _register()
    login_res = _login()
    token = login_res["token"]
    req = _make_request(token)
    user = require_auth(req)
    assert user["user_id"]
    assert user["account"] == "authtest"
    # 缓存命中路径
    assert validate_token_cached(token)["user_id"] == user["user_id"]
    # extract_token
    assert extract_token(req) == token


def test_require_auth_rejects_bad_token():
    from api.auth import require_auth
    import pytest
    with pytest.raises(Exception):
        require_auth(_make_request("not-a-real-token"))


def test_get_current_user_none_when_no_token():
    from api.auth import get_current_user
    assert get_current_user(_make_request(None)) is None


def test_api_me_requires_auth():
    from fastapi.testclient import TestClient
    from api.fastapi_server import app
    client = TestClient(app)
    # 未登录 401
    r = client.get("/api/me")
    assert r.status_code == 401
    # 登录后返回用户
    _register("metest")
    tok = _login("metest")["token"]
    r = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["account"] == "metest"


def test_app_redirects_when_unauthenticated():
    from fastapi.testclient import TestClient
    from api.fastapi_server import app
    client = TestClient(app)
    r = client.get("/app", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def test_update_profile_changes_nickname():
    """POST /api/profile 改昵称后，GET /api/me 立即返回新昵称。"""
    from fastapi.testclient import TestClient
    from api.fastapi_server import app
    client = TestClient(app)
    acct = _uniq_account("prof")
    _register(acct)
    tok = _login(acct)["token"]
    h = {"Authorization": f"Bearer {tok}"}
    r = client.post("/api/profile", json={"nickname": "新昵称"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("nickname") == "新昵称"
    # GET /api/me 应立即反映新昵称
    me = client.get("/api/me", headers=h).json()
    assert me.get("nickname") == "新昵称"


def test_change_password_wrong_old():
    """旧密码错误时 /api/password 返回失败且文案含“旧密码错误”。"""
    from fastapi.testclient import TestClient
    from api.fastapi_server import app
    client = TestClient(app)
    acct = _uniq_account("pwd")
    _register(acct)
    tok = _login(acct)["token"]
    h = {"Authorization": f"Bearer {tok}"}
    r = client.post(
        "/api/password",
        json={"old_password": "wrong", "new_password": "NewPass1234!"},
        headers=h,
    )
    assert r.status_code == 400
    assert "旧密码错误" in r.json().get("error", "")


def test_change_password_success_then_login_with_new():
    """改密成功后，用新密码可登录、旧密码不可登录。"""
    acct = _uniq_account("pwok")
    _register(acct)
    user_id = _login(acct)["user_id"]
    res = user_db_mod.user_db.change_password(
        user_id, "Test1234!", "NewPass1234!"
    )
    assert res.get("success") is True
    # 新密码登录成功
    new_login = user_db_mod.user_db.login(acct, "NewPass1234!")
    assert new_login.get("success") is True
    # 旧密码登录失败
    old_login = user_db_mod.user_db.login(acct, "Test1234!")
    assert old_login.get("success") is not True


def test_logout_invalidates_cache():
    """登出后立即从短缓存逐出该 token，validate_token_cached 返回 None。"""
    from api.auth import validate_token_cached
    from fastapi.testclient import TestClient
    from api.fastapi_server import app
    client = TestClient(app)
    acct = _uniq_account("logout")
    _register(acct)
    tok = _login(acct)["token"]
    h = {"Authorization": f"Bearer {tok}"}
    # 先访问 /api/me，使 token 进入短缓存
    assert client.get("/api/me", headers=h).status_code == 200
    assert validate_token_cached(tok) is not None  # 缓存已命中
    # 登出
    r = client.post("/api/logout", headers=h)
    assert r.status_code == 200
    assert r.json().get("success") is True
    # 缓存应被逐出：validate_token_cached 不再返回旧 user
    assert validate_token_cached(tok) is None
