from langchain.agents import AgentState
from langgraph.runtime import Runtime

from agent.utils.logger_handler import logger
from langchain.agents.middleware import wrap_tool_call, before_model, dynamic_prompt, ModelRequest
from typing import Callable
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent.utils.prompt_loader import load_report_prompts, load_system_prompts


@wrap_tool_call
def monitor_tool(
        #请求的数据封装
        request:ToolCallRequest,
        #执行的函数本身
        handler:Callable[[ToolCallRequest], ToolMessage | Command],

) -> ToolMessage | Command:
    logger.info(f"monitor_tool called with {request.tool_call['name']}")
    logger.info(f"monitor_tool called with parameters :{request.tool_call['args']}")

    try:
        result = handler(request)
        logger.info(f"monitor_tool called with result :{result}")

        if request.tool_call['name'] == 'fill_report_context_for_report':
            request.runtime.context["report"] = True
        return result
    except Exception as e:
        logger.error(f"monitor_tool called with exception :{str(e)}")
        raise e

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
