import secrets as _secrets
import time as _time

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
    import pytest

    from api.auth import require_auth
    with pytest.raises(Exception) as e:
        require_auth(_make_request(None))
    assert "401" in str(e.value) or e.value.status_code == 401


def test_require_auth_accepts_valid_token():
    from api.auth import extract_token, require_auth, validate_token_cached
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
    import pytest

    from api.auth import require_auth
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
    from fastapi.testclient import TestClient

    from api.auth import validate_token_cached
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


def test_login_sets_auth_cookie():
    """回归：POST /api/login 必须写回 token cookie，供页面级导航 /app 鉴权。

    修复前只把 token 放在响应体，/app 是浏览器页面导航、不携带 Authorization
    头，服务端无法识别会话 → 302 回落地页，与 /api/me（带 header）形成重定向循环。
    """
    from fastapi.testclient import TestClient

    from api.fastapi_server import app
    client = TestClient(app)
    acct = _uniq_account("cklogin")
    _register(acct)
    r = client.post("/api/login", json={"account": acct, "password": "Test1234!"})
    assert r.status_code == 200
    cookie_tok = r.cookies.get("token")
    assert cookie_tok, "登录未写入 token cookie"
    # cookie 中的 token 必须与本次响应的 token 一致（注意：每次登录都会刷新 token）
    assert cookie_tok == r.json().get("token"), "cookie 中的 token 应与本次响应体一致"


def test_app_served_when_authenticated_via_cookie():
    """回归：已登录用户仅用 cookie（不带 Authorization 头）导航 /app 应 200。

    这是之前死循环的核心场景——浏览器直接访问 /app 不会带 Authorization 头，
    修复前被误判未登录而 302。现在 extract_token 回退到 cookie，应正常返回页面。
    """
    from fastapi.testclient import TestClient

    from api.fastapi_server import app
    client = TestClient(app)
    acct = _uniq_account("ckapp")
    _register(acct)
    _login(acct)["token"]
    # 登录拿 cookie（沿用同一 client，自动携带 cookie）
    client.post("/api/login", json={"account": acct, "password": "Test1234!"})
    # 不带 Authorization 头导航 /app
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 200, f"/app 仍被重定向 -> {r.headers.get('location')}"
    assert "text/html" in r.headers.get("content-type", "")


def test_logout_clears_auth_cookie():
    """回归：POST /api/logout 应清除 token cookie，避免登出后凭 cookie 绕过鉴权。"""
    from fastapi.testclient import TestClient

    from api.fastapi_server import app
    client = TestClient(app)
    acct = _uniq_account("ckout")
    _register(acct)
    _login(acct)["token"]
    client.post("/api/login", json={"account": acct, "password": "Test1234!"})
    assert client.cookies.get("token"), "登录后应有 token cookie"
    # 登出
    client.post("/api/logout")
    # delete_cookie 后该 cookie 应从 client 中被移除
    assert not client.cookies.get("token"), "登出后 token cookie 应被清除"


def test_app_no_redirect_loop_when_authenticated():
    """回归：已登录下 /api/me(200) → /app(200) 不应再出现 302 循环。"""
    from fastapi.testclient import TestClient

    from api.fastapi_server import app
    client = TestClient(app)
    acct = _uniq_account("loop")
    _register(acct)
    _login(acct)["token"]
    client.post("/api/login", json={"account": acct, "password": "Test1234!"})
    me = client.get("/api/me", follow_redirects=False)
    app_page = client.get("/app", follow_redirects=False)
    assert me.status_code == 200
    assert app_page.status_code == 200, "仍存在 /app 重定向循环"


def test_logout_clears_session_token_stats(monkeypatch):
    """plan §4.2⑥ 收口：登出请求带 session_id 时，同步清理该会话的 token 统计。"""
    from fastapi.testclient import TestClient

    import api.routes.users as users_mod
    from api.fastapi_server import app
    cleared = []

    class _FakeCounter:
        def clear_session(self, sid):
            cleared.append(sid)

    # raising=False：接缝尚未实现时以行为断言失败（RED），而非 setup 报错
    monkeypatch.setattr(users_mod, "get_token_counter", lambda: _FakeCounter(),
                        raising=False)
    client = TestClient(app)
    acct = _uniq_account("logoutsid")
    _register(acct)
    tok = _login(acct)["token"]
    r = client.post("/api/logout", json={"session_id": "s42"},
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert cleared == ["s42"]


def test_logout_without_session_id_skips_counter_cleanup(monkeypatch):
    """未带 session_id（旧客户端）时不得误清任何会话统计。"""
    from fastapi.testclient import TestClient

    import api.routes.users as users_mod
    from api.fastapi_server import app
    cleared = []

    class _FakeCounter:
        def clear_session(self, sid):
            cleared.append(sid)

    monkeypatch.setattr(users_mod, "get_token_counter", lambda: _FakeCounter(),
                        raising=False)
    client = TestClient(app)
    acct = _uniq_account("logoutnosid")
    _register(acct)
    tok = _login(acct)["token"]
    r = client.post("/api/logout", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert cleared == []


def test_bearer_only_request_refreshes_cookie():
    """回归：仅带 Authorization: Bearer（前端 localStorage token）、无 cookie 的请求，
    中间件应自动补种 token cookie，使随后页面导航 /app 通过。

    覆盖真实场景：旧版本从不下发 cookie，已登录用户的 token 只在 localStorage，
    导航 /app 时服务端读不到会话而 302。刷新鉴权（如落地页 initAuthState 调 /api/me）
    后中间件补种 cookie，/app 方能加载。
    """
    from fastapi.testclient import TestClient

    from api.fastapi_server import app
    acct = _uniq_account("bearer")
    _register(acct)
    tok = _login(acct)["token"]
    # 全新 client（无登录 cookie），仅用 Bearer header 调 /api/me
    client = TestClient(app)
    me = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
    assert me.status_code == 200
    # 中间件应已补种 cookie
    assert client.cookies.get("token"), "Bearer-only 请求后未补种 cookie"
    # 随后导航 /app（client 自动携带 cookie）应 200
    app_page = client.get("/app", follow_redirects=False)
    assert app_page.status_code == 200, "补种 cookie 后 /app 仍被重定向"
