"""SSE 客户端断连的服务端取消测试（CancelToken 协作式取消链路）。

docs/IMPROVEMENT_DIRECTIONS.md 方向#4：客户端中断 SSE 后原实现后台线程仍
跑完整任务。修复链路：generate() 检测断连（心跳必检/每 20 chunk 抽检）→
cancel() → _stream_with_heartbeat 停止消费 → ReactAgent 流循环与
PlannerAgent 步骤边界协作式退出 → 不发 [DONE]、不写记忆。
"""
import asyncio
import threading
import time
import unittest

from langchain_core.messages import AIMessage

from agent.react_agent import ReactAgent
from api.sse import _stream_with_heartbeat
from utils.cancel_token import (
    CancelToken,
    PipelineCancelledError,
    raise_if_cancelled,
    reset_cancel_token,
    set_cancel_token,
)


class CancelTokenTests(unittest.TestCase):
    def test_cancel_lifecycle(self):
        token = CancelToken()
        self.assertFalse(token.cancelled)
        token.cancel()
        self.assertTrue(token.cancelled)

    def test_raise_if_cancelled_with_token(self):
        token = CancelToken()
        ct = set_cancel_token(token)
        try:
            raise_if_cancelled()  # 未取消 → no-op
            token.cancel()
            with self.assertRaises(PipelineCancelledError):
                raise_if_cancelled()
        finally:
            reset_cancel_token(ct)

    def test_raise_if_cancelled_without_token_is_noop(self):
        # 无通道（如同步 /api/analysis 路径）不抛
        raise_if_cancelled()


def test_stream_with_heartbeat_stops_on_cancel():
    """token 置位后停止消费：已投递的 chunk 照常，后续不再下发。"""
    token = CancelToken()
    release = threading.Event()

    def gen():
        yield "第一句。"
        token.cancel()   # 模拟主协程在首个 chunk 之后发现断连
        release.wait(2)  # 生产者仍卡在"长工具"里
        yield "不应被下发的第二句。"

    async def scenario():
        chunks = []
        async for kind, value in _stream_with_heartbeat(
                gen, "data: [KEEPALIVE]\n\n", interval=0.1, cancel_token=token):
            chunks.append((kind, value))
        return chunks

    try:
        chunks = asyncio.run(scenario())
    finally:
        release.set()
    texts = [v for k, v in chunks if k == "chunk"]
    # 取消与首 chunk 投递存在竞态：首句允许已下发或被丢弃，第二句绝不下发
    assert texts in ([], ["第一句。"])


def test_stream_with_heartbeat_heartbeat_path_breaks_on_cancel():
    """生产者卡死（无产出）时，心跳间隔处发现取消即退出，不无限心跳。"""
    token = CancelToken()
    block = threading.Event()

    def gen():
        block.wait(5)
        yield "永不产出"

    async def scenario():
        token.cancel()  # 已取消：第一次 timeout 即退出
        seen = []
        async for kind, value in _stream_with_heartbeat(
                gen, "data: [KEEPALIVE]\n\n", interval=0.05, cancel_token=token):
            seen.append(kind)
        return seen

    try:
        assert asyncio.run(scenario()) == []
    finally:
        block.set()


def test_stream_with_heartbeat_swallows_error_after_cancel():
    """取消后生产者线程的收尾异常不再向主协程抛（响应已在关闭中）。"""
    token = CancelToken()
    release = threading.Event()

    def gen():
        yield "开头。"
        release.wait(0.3)  # 本测试从不 set release：等短超时即可触发收尾异常（原 2s 纯属等待浪费）
        token.cancel()
        raise RuntimeError("producer 收尾爆炸")

    async def scenario():
        out = []
        async for kind, value in _stream_with_heartbeat(
                gen, "data: [KEEPALIVE]\n\n", interval=0.05, cancel_token=token):
            out.append(value)
        return out

    try:
        out = asyncio.run(scenario())  # 不应抛 RuntimeError
    finally:
        release.set()
    assert "开头。" in out


def test_react_agent_stream_loop_stops_on_cancel():
    class _FakeAgent:
        def stream(self, input_dict, stream_mode=None, context=None):
            yield {"messages": [AIMessage(content="第一段。")]}
            token.cancel()
            yield {"messages": [AIMessage(content="第二段不应出现。")]}

    token = CancelToken()
    agent = ReactAgent.__new__(ReactAgent)  # 绕过重型 __init__（真模型）
    agent.agent = _FakeAgent()

    out = list(agent.execute_stream("查询", user_id="u1", session_id="",
                                    cancel_token=token))
    assert out == ["第一段。\n"]


def test_chat_cancel_skips_done_and_memory(client, auth_headers, swap_srv_seam):
    """/api/chat 取消路径：不下发 [DONE]、不写记忆（残缺回复不入库）。"""
    from tests.test_chat_sse_api import _FakeMemoryService

    class _CancellingAgent:
        def execute_stream(self, query, history=None, user_id=None, session_id=None,
                           progress_emitter=None, cancel_token=None):
            yield "开始输出。"
            time.sleep(0.05)  # 让消费方先把首句下发，避免取消竞态吞掉首句
            cancel_token.cancel()  # 模拟断连已被发现（生产者视角）
            yield "这段已入队但会被取消检查丢弃。"

    mem = _FakeMemoryService()
    swap_srv_seam("_get_memory_service", lambda *a, **k: mem)
    swap_srv_seam("_get_react_agent", lambda uid: _CancellingAgent())

    r = client.post("/api/chat", json={"query": "测试"}, headers=auth_headers)

    assert r.status_code == 200
    assert "data: 开始输出。\n\n" in r.text
    assert "[DONE]" not in r.text
    assert "这段已入队" not in r.text
    assert mem.end_calls == []
