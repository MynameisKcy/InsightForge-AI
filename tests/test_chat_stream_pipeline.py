"""api/chat_stream.py 单元测试：纯函数表测 + 流翻译核心直测。

与 tests/test_chat_sse_api.py 的分工：那边经 TestClient 钉住 /api/chat
端到端线协议（字节级不变的重构安全网）；这里绕开 HTTP 层，直接以假
agent/memory/is_disconnected 注入，覆盖端点测试够不着的分支——
断连采样（心跳期/每 20 chunk）、取消路径三不做、图表 diff、事件路由。
"""
import asyncio
import json

import pytest

from api.chat_stream import (
    diff_new_charts,
    progress_event_token,
    snapshot_charts,
    stream_chat_sse,
    thinking_token,
)

# ── 纯函数：进度事件路由 ──

def test_metrics_event_routed_to_metrics_token():
    ev = {"type": "metrics", "input_tokens": 12}
    assert progress_event_token(ev) == \
        f"data: [METRICS:{json.dumps(ev, ensure_ascii=False)}]\n\n"


def test_decision_event_routed_to_decision_token():
    ev = {"type": "decision", "tool": "run_full_analysis"}
    assert progress_event_token(ev) == \
        f"data: [DECISION:{json.dumps(ev, ensure_ascii=False)}]\n\n"


def test_other_event_defaults_to_step_token():
    ev = {"type": "step", "index": 1, "name": "sql_query"}
    assert progress_event_token(ev) == \
        f"data: [STEP:{json.dumps(ev, ensure_ascii=False)}]\n\n"


# ── 纯函数：THINKING 切片 ──

def test_thinking_token_strips_marker_keeps_frame():
    assert thinking_token("[THINKING]解析问题中") == "data: [THINKING]解析问题中\n\n"


def test_non_thinking_chunk_returns_none():
    assert thinking_token("普通正文。") is None


# ── 图表快照 / diff ──

def test_snapshot_and_diff_new_charts(tmp_path):
    # 目录须在 reports/ 之下：_to_web_path 按 /reports/ 段提取 web URL
    charts = tmp_path / "reports" / "charts"
    charts.mkdir(parents=True)
    (charts / "old.html").write_text("<html>o</html>", encoding="utf-8")
    (charts / "ignored.png").write_bytes(b"\x89PNG")

    before = snapshot_charts(str(charts))
    (charts / "new.html").write_text("<html>n</html>", encoding="utf-8")

    new_urls = diff_new_charts(str(charts), before)
    assert new_urls == ["/reports/charts/new.html"]


def test_diff_empty_when_no_new_charts(tmp_path):
    charts = tmp_path / "charts"
    charts.mkdir()
    before = snapshot_charts(str(charts))
    assert diff_new_charts(str(charts), before) == []


def test_snapshot_missing_dir_returns_empty_set(tmp_path):
    assert snapshot_charts(str(tmp_path / "nope")) == set()


# ── 流翻译核心：直测（假 agent/memory/is_disconnected）──

class _FakeMemory:
    def __init__(self):
        self.end_calls = []

    def end_turn(self, user_id, session_id, query, response, input_tokens=None):
        self.end_calls.append({"response": response, "input_tokens": input_tokens})


class _FakeAgent:
    def __init__(self, chunks=(), exc=None, on_start=None):
        self.chunks = list(chunks)
        self.exc = exc
        self.on_start = on_start
        self._last_input_tokens = 7

    def execute_stream(self, query, history=None, user_id=None,
                       session_id=None, progress_emitter=None, cancel_token=None):
        if self.on_start:
            self.on_start(progress_emitter)
        yield from self.chunks
        if self.exc:
            raise self.exc


def _collect(agen):
    """收集异步生成器的全部输出（无 asyncio 插件，手动跑 loop）。"""

    async def _run():
        return [item async for item in agen]

    return asyncio.run(_run())


def _stream(agent, mem, *, disconnected=False, cancel=None, charts_dir="",
            interval=15.0):
    from utils.cancel_token import CancelToken
    if cancel is None:
        cancel = CancelToken()

    async def _is_disc():
        return disconnected

    return _collect(stream_chat_sse(
        agent=agent, memory_service=mem, query="分析", user_id="u1",
        session_id="s1", mem_context=[], new_session=False,
        cancel=cancel, is_disconnected=_is_disc,
        charts_dir=charts_dir, heartbeat_interval=interval,
    ))


def test_stream_happy_path_frames_and_persists():
    mem = _FakeMemory()
    out = _stream(_FakeAgent(["[THINKING]想", "第一句。第二句。"]), mem)

    assert out[0] == "data: [SESSION]s1\n\n"
    assert "data: [THINKING]想\n\n" in out
    # 按句拆分逐个下发
    assert "data: 第一句。\n\n" in out
    assert "data: 第二句。\n\n" in out
    assert out[-1] == "data: [DONE]\n\n"
    # 入记忆的是纯正文（[THINKING] 不落库）+ 实测 token
    assert len(mem.end_calls) == 1
    assert mem.end_calls[0]["response"] == "第一句。第二句。"
    assert mem.end_calls[0]["input_tokens"] == 7


def test_stream_progress_events_routed():
    mem = _FakeMemory()

    def _emit(emitter):
        emitter.emit("step", {"index": 1, "name": "sql_query"})

    out = _stream(_FakeAgent(["好。"], on_start=_emit), mem)
    assert any(line.startswith("data: [STEP:") for line in out)


def test_stream_error_yields_error_token_no_done_no_memory():
    mem = _FakeMemory()
    out = _stream(_FakeAgent(["部分。"], exc=RuntimeError("boom")), mem)

    assert "data: [ERROR] boom\n\n" in out
    assert not any("[DONE]" in line for line in out)
    assert mem.end_calls == []


def test_stream_pre_cancelled_skips_done_and_memory():
    from utils.cancel_token import CancelToken
    cancel = CancelToken()
    cancel.cancel()
    mem = _FakeMemory()
    out = _stream(_FakeAgent(["内容。"]), mem, cancel=cancel)

    assert not any("[DONE]" in line for line in out)
    assert mem.end_calls == []
    # 取消后不再下发正文
    assert not any("内容。" in line for line in out)


# 断连场景：消费侧提前返回后测试 loop 关闭，而 api/sse.py 桥的生产者线程
# 收尾会无条件再向队列 put（生产环境 uvicorn loop 常驻，永不触发）——
# 该线程竞态警告是测试环境的固有现象，非被测代码缺陷。
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_stream_disconnect_on_heartbeat_cancels_and_aborts():
    mem = _FakeMemory()
    from utils.cancel_token import CancelToken
    cancel = CancelToken()
    # 生产者先出一块然后阻塞，消费侧在心跳间隔检测断连
    class _SlowAgent(_FakeAgent):
        def execute_stream(self, *a, **k):
            yield "唯一一块。"
            import time
            time.sleep(0.4)

    agent = _SlowAgent()
    out = _stream(agent, mem, disconnected=True, cancel=cancel, interval=0.05)

    assert not any("[DONE]" in line for line in out)
    assert mem.end_calls == []          # 断连不入记忆
    assert cancel.cancelled             # 已通知生产者止损
    # 心跳帧不再下发给已断开的客户端
    assert "[KEEPALIVE]" not in "".join(out)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_stream_disconnect_sampled_every_20_chunks():
    mem = _FakeMemory()
    from utils.cancel_token import CancelToken
    cancel = CancelToken()
    chunks = [f"第{i}句。" for i in range(25)]

    out = _stream(_FakeAgent(chunks), mem, disconnected=True, cancel=cancel)

    assert not any("[DONE]" in line for line in out)
    assert mem.end_calls == []
    assert cancel.cancelled


def test_stream_new_charts_emitted_and_embedded(tmp_path):
    charts = tmp_path / "reports" / "charts"
    charts.mkdir(parents=True)

    def _write_chart(_emitter):
        (charts / "trend.html").write_text("<html/>", encoding="utf-8")

    mem = _FakeMemory()
    out = _stream(_FakeAgent(["图好了。"], on_start=_write_chart),
                  mem, charts_dir=str(charts))

    url = "/reports/charts/trend.html"
    assert f"data: [CHART:{url}]\n\n" in "".join(out)
    assert out[-1] == "data: [DONE]\n\n"
    assert url in mem.end_calls[0]["response"]
