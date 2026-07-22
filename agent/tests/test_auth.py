import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database.user_db as user_db_mod


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
