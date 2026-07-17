import os
import sys

from langchain.agents import create_agent
from langchain_core.messages import AIMessage

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for path in (PROJECT_ROOT, PROJECT_PARENT):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from agent.model.factory import chat_model
    from agent.utils.prompt_loader import load_system_prompts
    from agent.agent.tools.agent_tools import (rag_sumarize,get_weather,get_user_id,get_user_location,
                                               get_current_month,get_external_data,fill_report_context_for_report,
                                               run_full_analysis,get_data_overview,quick_data_insight,
                                               get_chart_insights,get_customer_overview_tool,get_customer_stats_tool)
    from agent.agent.tools.middleware import monitor_tool,log_before_model,report_prompt_switch
except ModuleNotFoundError:
    from model.factory import chat_model
    from utils.prompt_loader import load_system_prompts
    from agent.tools.agent_tools import (rag_sumarize,get_weather,get_user_id,get_user_location,
                                         get_current_month,get_external_data,fill_report_context_for_report,
                                         run_full_analysis,get_data_overview,quick_data_insight,
                                         get_chart_insights,get_customer_overview_tool,get_customer_stats_tool)
    from agent.tools.middleware import monitor_tool,log_before_model,report_prompt_switch


class ReactAgent:
    def __init__(self):
        self.agent = create_agent(
            model=chat_model,
            system_prompt=load_system_prompts(),
            tools=[rag_sumarize, get_weather, get_user_id, get_user_location,
                   get_current_month, get_external_data, fill_report_context_for_report,
                   run_full_analysis, get_data_overview, quick_data_insight,
                   get_chart_insights, get_customer_overview_tool, get_customer_stats_tool],
            middleware=[monitor_tool, log_before_model, report_prompt_switch],
        )

    def execute_stream(self, query: str, history: list[dict] | None = None,
                       user_id: str = "default", session_id: str = ""):
        # 构建完整上下文：历史消息 + 当前问题
        # 设置请求级 user_id，供下游 @tool 工具（run_full_analysis 等）读取，实现多用户隔离
        from utils.request_context import set_request_context, reset_request_context
        ctx_token = set_request_context(user_id=user_id, session_id=session_id)
        try:
            yield from self._execute_stream_inner(query, history)
        finally:
            reset_request_context(ctx_token)

    def _execute_stream_inner(self, query: str, history: list[dict] | None = None):
        # 构建完整上下文：历史消息 + 当前问题
        messages = []
        if history:
            for h in history:
                role = h.get("role", "user")
                content = h.get("content", "")
                if not content:
                    continue
                # 保持 system 角色不变（用于摘要等），其他统一为 user/assistant
                if role not in ("system", "user", "assistant"):
                    role = "user"
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": query})

        input_dict = {"messages": messages}

        # 内部推理模式的行首关键词，这些内容不应展示给用户
        _internal_patterns = [
            "让我", "我需要", "我注意到", "我将", "我先", "我可以", "我试试",
            "我来", "我看到", "根据数据", "让我来", "让我先", "接下来",
            "首先", "然后", "现在", "接下来我", "看来", "似乎",
            "I need", "Let me", "I'll", "I will", "I notice", "First",
            "Let's", "I can see", "It seems", "I should",
        ]

        def _is_internal_monologue(text: str) -> bool:
            """判断文本是否为内部推理而非最终回答。"""
            stripped = text.strip()
            if not stripped:
                return True
            for pat in _internal_patterns:
                if stripped.startswith(pat):
                    return True
            # 如果内容很短（<30字）且前面已经有思考过程，可能是过渡句
            if len(stripped) < 30 and any(w in stripped for w in ["工具", "查询", "分析", "tool", "query"]):
                return True
            return False

        local_tool_names = {
            "run_full_analysis": "正在运行完整数据分析...",
            "get_data_overview": "正在获取数据概况...",
            "quick_data_insight": "正在进行快速分析...",
            "rag_sumarize": "正在检索知识库...",
            "get_chart_insights": "正在检索图表知识库...",
            "get_external_data": "正在获取外部数据...",
            "get_weather": "正在查询天气...",
            "get_customer_overview_tool": "正在查询客户数据...",
            "get_customer_stats_tool": "正在统计客户分布...",
        }

        displayed_tool_messages = set()  # 避免重复显示同一工具的状态

        for chunk in self.agent.stream(input_dict, stream_mode="values", context={"report": False}):
            messages = chunk.get("messages", [])
            if not messages:
                continue

            latest = messages[-1]

            # 检测工具调用：发送思考状态
            if isinstance(latest, AIMessage) and hasattr(latest, "tool_calls") and latest.tool_calls:
                for tc in latest.tool_calls:
                    tool_name = tc.get("name", "")
                    if tool_name in local_tool_names and tool_name not in displayed_tool_messages:
                        displayed_tool_messages.add(tool_name)
                        yield f"[THINKING]{local_tool_names[tool_name]}\n"

            # 检测工具返回结果
            from langchain_core.messages import ToolMessage
            if isinstance(latest, ToolMessage):
                tool_name = getattr(latest, "name", "")
                if tool_name in local_tool_names:
                    displayed_tool_messages.discard(tool_name)

            # 输出 AI 回复内容（过滤内部推理）
            if isinstance(latest, AIMessage) and latest.content:
                text = latest.content.strip()
                if _is_internal_monologue(text):
                    continue
                yield text + "\n"


if __name__ == '__main__':
    agent = ReactAgent()
    for chunk in agent.execute_stream("为我生成我的使用报告"):
        print(
            chunk, end="", flush=True
        )
