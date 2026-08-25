"""ProgressEmitter: 线程安全的单请求进度通道。

背景：SSE 的 `_stream_with_heartbeat` 把 `ReactAgent.execute_stream` 放进后台
线程执行。当 LLM 调用 `run_full_analysis` 时，`PlannerAgent.run()` 在该后台
线程内同步执行（可能数分钟），期间 ReactAgent 的流式循环被阻塞、不 yield
任何 chunk，主协程只能靠心跳保活——用户看不到"走到哪一步了"。

本模块让 PlannerAgent 把步骤事件直接推入 `_stream_with_heartbeat` 的
asyncio.Queue（经 `loop.call_soon_threadsafe`），从而绕过被阻塞的
ReactAgent yield，把 `[STEP]` 事件实时送到前端。

contextvar：`set_progress_emitter` / `get_progress_emitter`。`execute_stream`
在后台线程内 set，`PlannerAgent.run`（同线程）get，可见性正确——与既有
`request_context` 多用户隔离的 contextvar 用法一致。非流式路径
（如 `/api/analysis`）未 set，emitter 为 None，`run()` 自动 no-op。
"""
import contextvars
from typing import Any, Optional

_progress_emitter: "contextvars.ContextVar[ProgressEmitter | None]" = \
    contextvars.ContextVar("progress_emitter", default=None)


def set_progress_emitter(emitter: Optional["ProgressEmitter"]):
    """绑定当前请求的进度通道，返回 contextvar token 供 reset。"""
    return _progress_emitter.set(emitter)


def reset_progress_emitter(token: Any) -> None:
    """恢复到上一个 emitter（通常为 None）。"""
    if token is None:
        return
    try:
        # reset 是 ContextVar 的方法（token 是 _contextvars.Token，无 reset；
        # 旧实现 token.reset() 恒 AttributeError 被吞，emitter 从不真正解绑）
        _progress_emitter.reset(token)
    except Exception:
        pass


def get_progress_emitter() -> Optional["ProgressEmitter"]:
    return _progress_emitter.get()


def emitter_bridge(channel: str):
    """组合根接线用：生成把 payload 推到当前请求进度通道的发布器。

    供 token_counter / decision_log 经 set_*_publisher 注入，使观测模块
    不感知 SSE 传输（见架构评审 R2 候选4）。无进度通道/推送失败均静默。
    """
    def _publish(payload: dict) -> None:
        emitter = get_progress_emitter()
        if emitter is not None:
            emitter.emit(channel, payload)
    return _publish


class ProgressEmitter:
    """线程安全的进度发射器：后台线程 emit -> 主协程 asyncio.Queue。"""

    def __init__(self):
        self._loop = None
        self._queue = None  # 主协程的 asyncio.Queue
        self._closed = False

    def bind(self, loop, queue) -> None:
        """由主协程绑定 event loop 与 asyncio.Queue，emit 据此跨线程投递。"""
        self._loop = loop
        self._queue = queue

    def emit(self, event_type: str, data: dict | None = None) -> None:
        """发射一个进度事件。无绑定/已关闭时静默 no-op。"""
        if self._closed or self._loop is None or self._queue is None:
            return
        payload = {"type": event_type}
        if data:
            payload.update(data)
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, ("progress", payload))
        except Exception:
            pass

    def close(self) -> None:
        self._closed = True
