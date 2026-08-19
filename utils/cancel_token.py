"""CancelToken: 单请求的协作式取消通道。

背景：SSE 客户端断连后（浏览器关闭/网络中断/用户停止生成），原实现里后台
线程仍会把 PlannerAgent 全流水线与后续 LLM 调用跑完，白白消耗 token 与
算力。Token 由 /api/chat 的 generate() 持有：主协程检测到断连即 cancel()；
生产者线程（ReactAgent 流循环、PlannerAgent 步骤边界）在各自的自然边界
轮询 cancelled，尽早退出。

不抢占：单次进行中的 LLM 调用仍会完成（LangChain 无安全中断 API），取消
发生在"下一个边界"——这与 DuckDB 超时 watchdog 的硬中断是两种互补手段。

contextvar：与 progress_emitter 相同模式——execute_stream 在生产者线程内
set，同线程的 @tool 工具 / PlannerAgent.run get，可见性正确（后台闲置
finalize 等其他线程拿不到、也不应拿到——取消只属于发起它的那个请求）。
"""
import contextvars
import threading


class PipelineCancelledError(RuntimeError):
    """流水线被取消（SSE 客户端断连）。生产者线程侧抛出，非致命。"""


class CancelToken:
    """线程安全的单请求取消标记：主协程 set，生产者线程 poll。"""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


_cancel_token: "contextvars.ContextVar[CancelToken | None]" = \
    contextvars.ContextVar("cancel_token", default=None)


def set_cancel_token(token: CancelToken | None):
    """绑定当前请求的取消通道，返回 token 供 reset。"""
    return _cancel_token.set(token)


def reset_cancel_token(token) -> None:
    if token is None:
        return
    try:
        # reset 是 ContextVar 的方法（token 是 _contextvars.Token，无 reset）
        _cancel_token.reset(token)
    except Exception:
        pass


def get_cancel_token() -> CancelToken | None:
    return _cancel_token.get()


def raise_if_cancelled() -> None:
    """协作式取消检查点：已取消则 raise PipelineCancelledError；无通道时 no-op。"""
    token = _cancel_token.get()
    if token is not None and token.cancelled:
        raise PipelineCancelledError("客户端已断开连接，分析已取消")
