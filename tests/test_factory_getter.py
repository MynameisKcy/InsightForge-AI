import model.factory as fac


def test_same_user_returns_same_instance(monkeypatch):
    """同一 user_id 重复取用 -> 同一实例（缓存命中）。"""
    # 避免 DB：_load_user_override 对无配置用户返回 {}
    monkeypatch.setattr(fac, "_load_user_override", lambda uid: {})
    fac.reload_model_config("u_cache_1")
    a = fac.get_chat_model("u_cache_1")
    assert fac.get_chat_model("u_cache_1") is a


def test_reload_evicts_and_rebuilds(monkeypatch):
    """reload_model_config 失效该用户缓存 -> 下次取用得到新实例。"""
    monkeypatch.setattr(fac, "_load_user_override", lambda uid: {})
    fac.reload_model_config("u_cache_2")
    a = fac.get_chat_model("u_cache_2")
    fac.reload_model_config("u_cache_2")  # 模拟配置保存
    b = fac.get_chat_model("u_cache_2")
    assert b is not a  # 缓存被失效 -> 重建新实例


def test_different_users_get_different_instances(monkeypatch):
    """不同 user_id -> 不同实例（多用户隔离）。"""
    monkeypatch.setattr(fac, "_load_user_override", lambda uid: {})
    fac.reload_model_config("u_cache_3")
    fac.reload_model_config("u_cache_4")
    a = fac.get_chat_model("u_cache_3")
    b = fac.get_chat_model("u_cache_4")
    assert a is not b
