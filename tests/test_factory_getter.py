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


def test_openai_path_carries_request_timeout(monkeypatch):
    """OpenAI 兼容端点(ChatOpenAI)必须带 request_timeout 兜底超时。

    回归:2026-09-03 live 观测到模型调用无超时→请求挂死→SSE producer
    永久阻塞(3 个 quick 并行后 7min+ 无日志、前端超时)。
    """
    monkeypatch.setattr(fac, "_load_user_override", lambda uid: {
        "llm_base_url": "http://mock-endpoint.invalid/v1",
        "llm_api_key": "sk-mock",
        "llm_model_name": "mock-model",
    })
    m = fac._build_chat_model("u_timeout_1")
    from langchain_openai import ChatOpenAI
    assert isinstance(m, ChatOpenAI)
    assert m.request_timeout == fac.LLM_REQUEST_TIMEOUT_S
    assert m.request_timeout > 0


def test_timeout_larger_than_slowest_normal_call():
    """兜底超时须大于当前最慢正常调用(SQL 生成实测 ~105s)，避免误杀。"""
    assert fac.LLM_REQUEST_TIMEOUT_S >= 150, "超时必须给 SQL 生成(实测~105s)留足余量"
