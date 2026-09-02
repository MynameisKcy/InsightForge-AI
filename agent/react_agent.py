
from langchain.agents import create_agent
from langchain_core.messages import AIMessage

from agent.tools.agent_tools import REPORT_MODE, for_intent
from agent.tools.intent_router import Intent
from agent.tools.middleware import (
    dynamic_toolset,
    log_before_model,
    monitor_tool,
    report_prompt_switch,
    trace_model_call,
)
from model.factory import get_chat_model
from utils.logger_handler import logger
from utils.prompt_loader import load_system_prompts
from utils.sse_protocol import inband_thinking


class ReactAgent:
    def __init__(self, user_id=None, model=None):
        # model 注入优先（测试/上层共用同一实例）；未注入按 user_id 解析（factory 缓存）
        # tools：绑定 analysis 档 = 工具目录全集（ADR-0004，见 agent_tools.for_intent）；
        # dynamic_toolset middleware 在每次模型调用前按 query 意图裁剪可见工具集
        # （query 档不暴露 run_full_analysis，chat 档只留 RAG + 报告态切换）。
        # 这样 ReAct 在 chat 意图下根本看不到 run_full_analysis，不会倾向选它；
        # analysis 意图下工具全集可见。
        self.agent = create_agent(
            model=model if model is not None else get_chat_model(user_id),
            system_prompt=load_system_prompts(),
            tools=for_intent(Intent.ANALYSIS),
            # 顺序：dynamic_toolset 在最前先裁 tools，再走 log_before_model / trace / report_prompt_switch
            # 报告态切换依赖 runtime.context[REPORT_MODE] 仍由 monitor_tool 写，
            # dynamic_toolset 不读/不改 report 标志。
            middleware=[dynamic_toolset, monitor_tool, log_before_model,
                        report_prompt_switch, trace_model_call],
        )

    def execute_stream(self, query: str, history: list[dict] | None = None,
                       user_id: str = "default", session_id: str = "",
                       progress_emitter=None, cancel_token=None):
        # 构建完整上下文：历史消息 + 当前问题
        # 设置请求级 user_id，供下游 @tool 工具（run_full_analysis 等）读取，实现多用户隔离
        # progress_emitter：绑定到 contextvar，供 PlannerAgent.run 在 run_full_analysis
        # 执行期间把步骤事件直接推入 SSE 队列（绕过被阻塞的流式 yield）。
        # cancel_token：客户端断连时由 /api/chat 主协程 cancel()；流循环在每次
        # agent.stream 产出后检查，尽早停止后续模型调用（协作式，不抢占进行中的调用）。
        from utils.cancel_token import reset_cancel_token, set_cancel_token
        from utils.progress_emitter import reset_progress_emitter, set_progress_emitter
        from utils.request_context import reset_request_context, set_request_context
        ctx_token = set_request_context(user_id=user_id, session_id=session_id)
        pe_token = set_progress_emitter(progress_emitter)
        ct_token = set_cancel_token(cancel_token)
        try:
            yield from self._execute_stream_inner(query, history, user_id, session_id,
                                                  cancel_token=cancel_token)
        finally:
            reset_cancel_token(ct_token)
            reset_progress_emitter(pe_token)
            reset_request_context(ctx_token)

    def _execute_stream_inner(self, query: str, history: list[dict] | None = None,
                              user_id: str = "default", session_id: str = "",
                              cancel_token=None):
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
            "get_customer_overview_tool": "正在查询客户数据...",
            "get_customer_stats_tool": "正在统计客户分布...",
        }

        displayed_tool_messages = set()  # 避免重复显示同一工具的状态

        # 追踪最后一次模型调用的 AIMessage，用于抽取实测 input_tokens（ADR-0003 Phase 2）
        final_ai_msg = None

        for chunk in self.agent.stream(input_dict, stream_mode="values",
                                       context={REPORT_MODE: False}):
            if cancel_token is not None and cancel_token.cancelled:
                logger.info("Stream cancelled by client disconnect; stopping agent loop")
                break
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
                        yield inband_thinking(local_tool_names[tool_name])

            # 检测工具返回结果
            from langchain_core.messages import ToolMessage
            if isinstance(latest, ToolMessage):
                tool_name = getattr(latest, "name", "")
                if tool_name in local_tool_names:
                    displayed_tool_messages.discard(tool_name)

            # 输出 AI 回复内容（过滤内部推理：转决策事件推送前端，不再静默丢弃）
            if isinstance(latest, AIMessage) and latest.content:
                text = latest.content.strip()
                # 记录最近一次有内容的 AIMessage（最终答案，含完整上下文的 token 用量）
                final_ai_msg = latest
                if _is_internal_monologue(text):
                    if len(text) >= 10:   # 过短过渡句仍静默
                        try:
                            from utils.decision_log import (
                                emit_decision,
                                log_decision,
                                make_decision,
                            )
                            log_decision(make_decision(source="react_agent", reasoning=text[:500]))
                            emit_decision({"source": "react_agent", "reasoning": text[:500]})
                        except Exception:
                            pass
                    continue
                yield text + "\n"

        # 流结束后：提取实测 input_tokens，供调用方经 MemoryService.end_turn() 回灌
        # Session Memory。不再直接调用 get_session().record_input_tokens()，避免
        # react_agent 直接依赖 memory 模块（memory → agents 循环依赖已消除）。
        self._last_input_tokens = None
        if final_ai_msg is not None and session_id:
            try:
                from memory.context_budget import extract_input_tokens
                self._last_input_tokens = extract_input_tokens(final_ai_msg)
            except Exception as e:
                logger.warning(f"Failed to extract input tokens: {e}")


if __name__ == '__main__':
    agent = ReactAgent()
    for chunk in agent.execute_stream("为我生成我的使用报告"):
        print(
            chunk, end="", flush=True
        )
