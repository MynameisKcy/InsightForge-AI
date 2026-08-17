"""
请求级上下文：用 contextvars 在异步/同步调用链中传递 user_id / session_id。

为什么用 contextvars：
ReactAgent 的 LangChain @tool 工具签名无法直接接收 user_id，
而 contextvars 能在同一线程/任务的整个调用链中自然传递，不污染函数签名。
FastAPI 入口设置 user_id，工具内（agent_tools.py）读取，下传给 PlannerAgent/SQLAgent，
实现多用户数据层隔离。
"""

import contextvars

# 当前请求的 user_id（决定使用哪个 DuckDB 实例）
current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_user_id", default="default")

# 当前请求的 session_id（可选，用于记忆/会话隔离）
current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_session_id", default="")


def set_request_context(user_id: str = "default", session_id: str = "") -> contextvars.Token:
    """设置当前请求的 user_id/session_id，返回 token 供 reset 用。

    典型用法：
        token = set_request_context(user_id, session_id)
        try:
            ...执行...
        finally:
            reset_request_context(token)
    """
    t1 = current_user_id.set(user_id or "default")
    t2 = current_session_id.set(session_id or "")
    return (t1, t2)


def reset_request_context(token) -> None:
    """恢复上下文到 set 之前的状态。"""
    t1, t2 = token
    current_user_id.reset(t1)
    current_session_id.reset(t2)


def get_user_id() -> str:
    """获取当前请求的 user_id，默认 'default'。"""
    return current_user_id.get()


def get_session_id() -> str:
    """获取当前请求的 session_id。"""
    return current_session_id.get()
