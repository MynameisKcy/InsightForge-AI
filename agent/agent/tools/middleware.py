import time

from langchain.agents import AgentState
from langgraph.runtime import Runtime

from agent.utils.logger_handler import logger
from langchain.agents.middleware import wrap_tool_call, wrap_model_call, before_model, dynamic_prompt, ModelRequest
from typing import Callable
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent.utils.prompt_loader import load_report_prompts, load_system_prompts

try:
    from agent.utils.tracing import get_tracer, record_exception, record_usage
except ModuleNotFoundError:
    from utils.tracing import get_tracer, record_exception, record_usage


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
            span.set_attribute("duration_ms", round((time.perf_counter() - start) * 1000, 1))
            span.set_attribute("status", "success")
            # 结果摘要截 200，便于 Jaeger 中直接判断调用是否符合预期
            span.set_attribute("tool.result_summary", str(getattr(result, "content", result))[:200])
            logger.info(f"monitor_tool called with result :{result}")

            if tool_name == 'fill_report_context_for_report':
                request.runtime.context["report"] = True
            return result
        except Exception as e:
            record_exception(span, e)
            logger.error(f"monitor_tool called with exception :{str(e)}")
            raise e


@wrap_model_call
def trace_model_call(
        request: ModelRequest,
        handler: Callable[[ModelRequest], object],
):
    """ReactAgent 每次模型调用的追踪（决策 D5）：Span agent.reason + token usage。

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
            record_usage(span, getattr(msg, "usage_metadata", None))
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
    is_report = request.runtime.context.get("report",False)
    if is_report:
        return load_report_prompts()

    return load_system_prompts()
