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


# ── 思考模式旋钮 enable_thinking（默认关）──


def test_enable_thinking_default_off(monkeypatch):
    """无任何配置 -> 默认关：qwen3 系在兼容端点默认开思考，平台须兜底关闭。"""
    monkeypatch.delenv("ENABLE_THINKING", raising=False)
    assert fac._resolve_enable_thinking(None) is False
    assert fac._resolve_enable_thinking({}) is False


def test_enable_thinking_priority(monkeypatch):
    """用户配置 > .env：用户显式 false 压过 env true，显式 true 压过 env false。"""
    monkeypatch.setenv("ENABLE_THINKING", "true")
    assert fac._resolve_enable_thinking({"llm_enable_thinking": False}) is False
    assert fac._resolve_enable_thinking({}) is True
    monkeypatch.setenv("ENABLE_THINKING", "false")
    assert fac._resolve_enable_thinking({"llm_enable_thinking": "true"}) is True


def test_enable_thinking_loose_bool_parse(monkeypatch):
    """宽松布尔解析：true/1/yes/on 开；false/0/no/空串 关；None 回落 env。"""
    monkeypatch.delenv("ENABLE_THINKING", raising=False)
    for v in ("true", "True", "1", "yes", "on", True):
        assert fac._resolve_enable_thinking({"llm_enable_thinking": v}) is True
    for v in ("false", "0", "no", "", False):
        assert fac._resolve_enable_thinking({"llm_enable_thinking": v}) is False
    monkeypatch.setenv("ENABLE_THINKING", "true")
    assert fac._resolve_enable_thinking({"llm_enable_thinking": None}) is True


def test_openai_path_carries_enable_thinking_default_off(monkeypatch):
    """ChatOpenAI 分支必须注入 enable_thinking（默认 False）。

    回归：2026-09-03 差分实验实测意图分类被思考 token 拖到 2.5~7s，
    关思考后 0.43s，且仓库此前无任何 enable_thinking 配置。
    """
    monkeypatch.delenv("ENABLE_THINKING", raising=False)
    monkeypatch.setattr(fac, "_load_user_override", lambda uid: {
        "llm_base_url": "http://mock-endpoint.invalid/v1",
        "llm_api_key": "sk-mock",
        "llm_model_name": "mock-model",
    })
    m = fac._build_chat_model("u_think_1")
    assert m.extra_body == {"enable_thinking": False}


def test_openai_path_carries_enable_thinking_on(monkeypatch):
    """用户显式开启 -> extra_body 注入 True。"""
    monkeypatch.setattr(fac, "_load_user_override", lambda uid: {
        "llm_base_url": "http://mock-endpoint.invalid/v1",
        "llm_enable_thinking": True,
    })
    m = fac._build_chat_model("u_think_2")
    assert m.extra_body == {"enable_thinking": True}
