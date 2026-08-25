"""token_counter 契约测试（架构评审 R2 候选4：观测埋点收口）。

钉住三块契约：计价（价格表/env 覆盖/默认兜底）、account_response 双形态记账
（精确 usage / 缺失时估算）、发布器注入（SSE 解耦后 payload 形状与静默语义）。
全部用直接构造的 TokenCounter / monkeypatch 模块级单例，不触达真实 SSE。
"""
import pytest

import utils.token_counter as tc


class _Msg:
    """最小 LangChain 响应形态：content + usage_metadata + response_metadata。"""

    def __init__(self, content="out", usage=None, meta=None):
        self.content = content
        self.usage_metadata = usage
        self.response_metadata = meta or {}


class _StructuredResp:
    """ReactAgent 中间件返回形态：真消息挂在 structured_response 上。"""

    def __init__(self, msg):
        self.structured_response = msg


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    """每个测试拿到全新单例与无发布器状态，退出自动还原。

    _publisher 尚未实现时 raising=False 让测试以真实断言失败（而非 setup 报错）暴露缺口。
    """
    monkeypatch.setattr(tc, "_counter", None)
    monkeypatch.setattr(tc, "_publisher", None, raising=False)


# ── 计价 ──

def test_record_accumulates_by_price_table():
    counter = tc.TokenCounter()
    snap = counter.record("s1", "qwen-plus", 1000, 500)
    assert (snap.input_tokens, snap.output_tokens, snap.calls) == (1000, 500, 1)
    # (1000*0.0008 + 500*0.002) / 1000
    assert snap.estimated_cost_cny == pytest.approx(0.0018)


def test_record_env_prices_override_table(monkeypatch):
    monkeypatch.setenv("TOKEN_PRICE_INPUT", "0.01")
    monkeypatch.setenv("TOKEN_PRICE_OUTPUT", "0.02")
    snap = tc.TokenCounter().record("s1", "qwen-max", 100, 100)
    assert snap.estimated_cost_cny == pytest.approx((100 * 0.01 + 100 * 0.02) / 1000)


def test_record_unknown_model_falls_back_to_default_price():
    snap = tc.TokenCounter().record("s1", "mystery-model", 1000, 0)
    assert snap.estimated_cost_cny == pytest.approx(1000 * 0.0008 / 1000)


def test_record_zero_tokens_skipped():
    counter = tc.TokenCounter()
    assert counter.record("s1", "qwen-plus", 0, 0) is None
    assert counter.get_session_usage("s1") is None


def test_record_estimated_marks_call():
    counter = tc.TokenCounter()
    snap = counter.record_estimated("x" * 40, "y" * 8, "qwen-plus")
    assert (snap.input_tokens, snap.output_tokens) == (10, 2)
    assert (snap.calls, snap.estimated_calls) == (1, 1)


def test_get_session_usage_returns_snapshot_not_live_ref():
    counter = tc.TokenCounter()
    counter.record("s1", "qwen-plus", 10, 0)
    snap = counter.get_session_usage("s1")
    snap.input_tokens = 999          # 改快照不得影响内部累计
    assert counter.get_session_usage("s1").input_tokens == 10


def test_clear_session_removes_and_is_idempotent():
    counter = tc.TokenCounter()
    counter.record("s1", "qwen-plus", 10, 0)
    counter.clear_session("s1")
    assert counter.get_session_usage("s1") is None
    counter.clear_session("s1")      # 二次清理不抛


# ── 发布器注入（SSE 解耦）──

def test_publisher_receives_metrics_payload(monkeypatch):
    seen = []
    tc.set_metrics_publisher(seen.append)
    monkeypatch.setattr(tc, "get_session_id", lambda: "s1")
    tc.TokenCounter().record("s1", "qwen-plus", 10, 5)

    assert len(seen) == 1
    p = seen[0]
    assert (p["session_id"], p["model"]) == ("s1", "qwen-plus")
    assert (p["input_tokens"], p["output_tokens"]) == (10, 5)
    assert p["calls"] == 1 and p["estimated"] is False
    assert isinstance(p["cost_cny"], float)


def test_no_publisher_is_silent():
    tc.TokenCounter().record("s1", "qwen-plus", 10, 5)   # 不接线：静默丢弃，不抛
    assert True


def test_empty_session_id_records_but_not_published(monkeypatch):
    seen = []
    tc.set_metrics_publisher(seen.append)
    monkeypatch.setattr(tc, "get_session_id", lambda: "")
    snap = tc.TokenCounter().record("", "qwen-plus", 10, 5)
    assert snap.input_tokens == 10                        # 记账照常
    assert seen == []                                     # 只是不推送


def test_publisher_failure_does_not_break_accounting(monkeypatch):
    def boom(_):
        raise RuntimeError("sse down")
    tc.set_metrics_publisher(boom)
    snap = tc.TokenCounter().record("s1", "qwen-plus", 10, 5)
    assert snap.calls == 1                                # 推送失败不影响记账


# ── account_response（LLM 响应记账唯一入口）──

def test_account_response_precise_usage(monkeypatch):
    monkeypatch.setattr(tc, "get_session_id", lambda: "s9")
    counter = tc.get_token_counter()
    resp = _Msg(content="hi", usage={"input_tokens": 3, "output_tokens": 4},
                meta={"model_name": "qwen-turbo"})

    usage = tc.account_response(resp)

    assert usage == {"input_tokens": 3, "output_tokens": 4}
    s = counter.get_session_usage("s9")
    assert (s.input_tokens, s.output_tokens, s.estimated_calls) == (3, 4, 0)


def test_account_response_unwraps_structured_response(monkeypatch):
    monkeypatch.setattr(tc, "get_session_id", lambda: "s9")
    counter = tc.get_token_counter()
    inner = _Msg(usage={"input_tokens": 7, "output_tokens": 1},
                 meta={"model_name": "qwen-turbo"})

    usage = tc.account_response(_StructuredResp(inner))

    assert usage["input_tokens"] == 7
    assert counter.get_session_usage("s9").input_tokens == 7


def test_account_response_estimated_when_usage_missing(monkeypatch):
    monkeypatch.setattr(tc, "get_session_id", lambda: "s9")
    counter = tc.get_token_counter()
    resp = _Msg(content="out1234")   # 7 字符 → 输出估算 1 token

    usage = tc.account_response(resp, fallback_prompt="p" * 12)

    assert usage is None                                   # 无 usage 可写 span
    s = counter.get_session_usage("s9")
    assert (s.input_tokens, s.output_tokens) == (3, 1)
    assert s.estimated_calls == 1


def test_account_response_suppresses_internal_errors(monkeypatch):
    def broken():
        raise RuntimeError("no counter")
    monkeypatch.setattr(tc, "get_token_counter", broken)

    assert tc.account_response(_Msg()) is None             # 记账失败静默，业务无感
