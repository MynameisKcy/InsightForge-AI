import contextlib
import time
from collections.abc import Callable

from langchain.agents import AgentState
from langchain.agents.middleware import (
    ModelRequest,
    before_model,
    dynamic_prompt,
    wrap_model_call,
    wrap_tool_call,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

from utils.decision_log import emit_decision, log_decision, make_decision
from utils.logger_handler import logger
from utils.prompt_loader import load_report_prompts, load_system_prompts
from utils.request_context import get_session_id, get_user_id
from utils.tracing import get_tracer, record_exception, record_usage


def _log_tool_decision(tool_name: str, tool_args, duration_ms: float,
                       result_summary: str, error: str = "") -> None:
    """工具决策：JSONL 落盘 + SSE [DECISION] 推送（旁路能力，失败静默）。"""
    try:
        decision = make_decision(
            source="tool_call",
            tool_selected=tool_name,
            tool_args=tool_args if isinstance(tool_args, dict) else {"raw": str(tool_args)[:200]},
            execution_time_ms=round(duration_ms, 1),
            result_summary=(error or result_summary)[:200],
        )
        log_decision(decision)
        emit_decision({
            "source": "tool_call",
            "tool": tool_name,
            "timing_ms": decision.execution_time_ms,
            "args": decision.tool_args,
            "result_summary": decision.result_summary,
            "error": bool(error),
        })
    except Exception as e:
        logger.debug(f"decision log failed: {e}")


@wrap_tool_call
def monitor_tool(
        #请求的数据封装
        request:ToolCallRequest,
        #执行的函数本身
        handler:Callable[[ToolCallRequest], ToolMessage | Command],

) -> ToolMessage | Command:
    tool_name = request.tool_call['name']
    logger.info(f"monitor_tool called with {tool_name}")
    logger.info(f"monitor_tool called with parameters :{request.tool_call['args']}")

    tracer = get_tracer()
    with tracer.start_as_current_span(f"tool.{tool_name}") as span:
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("tool.args", str(request.tool_call['args'])[:500])
        start = time.perf_counter()
        try:
            result = handler(request)
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            span.set_attribute("duration_ms", duration_ms)
            span.set_attribute("status", "success")
            # 结果摘要截 200，便于 Jaeger 中直接判断调用是否符合预期
            result_summary = str(getattr(result, "content", result))
            span.set_attribute("tool.result_summary", result_summary[:200])
            _log_tool_decision(tool_name, request.tool_call['args'], duration_ms, result_summary)
            logger.info(f"monitor_tool called with result :{result}")

            if tool_name == 'fill_report_context_for_report':
                request.runtime.context["report"] = True
            return result
        except Exception as e:
            record_exception(span, e)
            _log_tool_decision(tool_name, request.tool_call['args'],
                               round((time.perf_counter() - start) * 1000, 1), "", error=str(e))
            logger.error(f"monitor_tool called with exception :{str(e)}")
            raise e


@wrap_model_call
def trace_model_call(
        request: ModelRequest,
        handler: Callable[[ModelRequest], object],
):
    """ReactAgent 每次模型调用的追踪：Span agent.reason + token usage 统计。

    handler 返回 ModelResponse（structured_response 为 AIMessage）或 AIMessage 本身，
    两种形态都从其上读 usage_metadata。
    """
    tracer = get_tracer()
    with tracer.start_as_current_span("agent.reason") as span:
        start = time.perf_counter()
        try:
            response = handler(request)
            span.set_attribute("duration_ms", round((time.perf_counter() - start) * 1000, 1))
            span.set_attribute("status", "success")
            msg = getattr(response, "structured_response", response)
            usage = getattr(msg, "usage_metadata", None)
            record_usage(span, usage)
            # Token 统计（session 累计 + SSE [METRICS] 推送）
            with contextlib.suppress(Exception):
                from utils.token_counter import get_token_counter
                model_name = (getattr(msg, "response_metadata", None) or {}).get("model_name", "")
                if usage:
                    get_token_counter().record_usage(usage, model_name)
                else:
                    content = str(getattr(msg, "content", ""))
                    get_token_counter().record_estimated(str(getattr(request, "system_prompt", "") or ""), content, model_name)
            return response
        except Exception as e:
            record_exception(span, e)
            raise

@before_model
def log_before_model(
        state:AgentState,
        runtime:Runtime,
):
    state_messages = state.get("messages", [])
    logger.info(f"[log_before_model] 即将调用模型，带有:{len(state_messages)}条消息")

    if state_messages:
        latest_message = state_messages[-1]
        if isinstance(latest_message, dict):
            latest_content = latest_message.get("content", "")
        else:
            latest_content = getattr(latest_message, "content", "")
        logger.debug(f"[log_before_model] {type(latest_message).__name__} | {str(latest_content).strip()}")
    return None


@dynamic_prompt #每一次在生成prompt之前，调用此函数
def report_prompt_switch(request:ModelRequest):
    return _build_system_prompt(request)


def _build_system_prompt(request: ModelRequest) -> str:
    """构建 system prompt：报告模式用报告 prompt；正常模式追加跨会话召回（ADR-0003 Phase 4）。

    召回并入单条 system prompt（不再另起 system 消息），由本函数统一管控：
    报告模式不注入历史会话记忆（避免泄漏），仅正常模式追加。可独立单测。
    """
    is_report = request.runtime.context.get("report", False)
    if is_report:
        return load_report_prompts()
    base = load_system_prompts()
    recall = _recall_for_turn(request)
    if recall:
        return f"{base}\n\n{recall}"
    return base


def _last_user_content(messages) -> str:
    """取消息列表中最后一条 user 消息的文本（兼容 dict / langchain BaseMessage）。"""
    for m in reversed(messages or []):
        if isinstance(m, dict):
            if m.get("role") == "user":
                return str(m.get("content", ""))
        elif getattr(m, "type", None) == "human":  # BaseMessage.type: human/ai/system
            return str(getattr(m, "content", ""))
    return ""


def _recall_for_turn(request: ModelRequest) -> str:
    """取本轮回话的跨会话召回，在 runtime.context 内缓存（同轮多次模型调用只 embed 一次）。

    runtime.context 每次 agent.stream 重建（见 execute_stream 传 context={"report": False}），
    故缓存天然按轮失效；contextvar 由 execute_stream 在同线程先设好，user_id/session_id 此处可取。
    召回在中间件里发生 -> 与 /api/chat 解耦，且报告模式不召回（见 _build_system_prompt）。
    """
    query = _last_user_content(getattr(request, "messages", None))
    if not query:
        return ""
    uid = get_user_id() or ""
    if not uid or uid == "default":
        # 未设 contextvar（非请求路径，如直接调 agent）时不召回，避免误用 default 用户记忆
        return ""
    sid = get_session_id() or ""
    cache = request.runtime.context.setdefault("_memory_recall", {})
    key = (uid, sid, query[:500])
    if key not in cache:
        try:
            from memory.recall import get_memory_recall
            cache[key] = get_memory_recall().recall(query, uid, exclude_session_id=sid or None)
        except Exception as e:
            logger.warning(f"recall in dynamic_prompt failed: {e}")
            cache[key] = ""
    return cache[key]
