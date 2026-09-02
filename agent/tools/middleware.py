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
from utils.token_counter import account_response
from utils.tracing import record_usage, traced


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

    # 集中式权限 hook（#4）：tool.invoke 拦截点。默认 hook 仅审计恒放行
    # （POINT_TOOL_INVOKE → None），行为不变；未来权限规则注册到该点即
    # 自动对所有工具生效，新增工具无需逐个补拦截。
    try:
        from utils.permission_hooks import POINT_TOOL_INVOKE, trigger_hooks
        reason = trigger_hooks(POINT_TOOL_INVOKE,
                               tool_name=tool_name,
                               args=request.tool_call.get('args', {}),
                               user_id=get_user_id())
    except Exception as e:
        reason = f"permission hook 异常: {e}"
    if reason:
        _log_tool_decision(tool_name, request.tool_call.get('args', {}), 0.0,
                           "", error=f"blocked: {reason}")
        logger.warning(f"tool.invoke blocked {tool_name}: {reason}")
        return ToolMessage(
            content=f"[权限拦截] 工具 {tool_name} 调用被拒绝：{reason}",
            name=tool_name,
            tool_call_id=request.tool_call.get('id', ''),
        )

    start = time.perf_counter()
    with traced(f"tool.{tool_name}", attrs={
            "tool.name": tool_name,
            "tool.args": str(request.tool_call['args'])[:500],
    }) as span:
        try:
            result = handler(request)
        except Exception as e:
            # 决策日志需错误分支的耗时数据，故此处自计；span 状态/异常由 traced 收尾
            _log_tool_decision(tool_name, request.tool_call['args'],
                               round((time.perf_counter() - start) * 1000, 1), "", error=str(e))
            logger.error(f"monitor_tool called with exception :{str(e)}")
            raise
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        # 结果摘要截 200，便于 Jaeger 中直接判断调用是否符合预期
        result_summary = str(getattr(result, "content", result))
        span.set_attribute("tool.result_summary", result_summary[:200])
        _log_tool_decision(tool_name, request.tool_call['args'], duration_ms, result_summary)
        logger.info(f"monitor_tool called with result :{result}")

        # 模式副作用：查目录表拿到 effect（context 键名）后通用置位。
        # ADR-0004 扩展：替代按工具名魔法串特判——工具改名只改目录表，
        # 报告模式不再因名字字符串失配而静默断裂。effect 即 context 键。
        effect = mode_effect_for(tool_name)
        if effect:
            request.runtime.context[effect] = True
        return result


@wrap_model_call
def trace_model_call(
        request: ModelRequest,
        handler: Callable[[ModelRequest], object],
):
    """ReactAgent 每次模型调用的追踪：Span agent.reason + token usage 统计。

    handler 返回 ModelResponse（structured_response 为 AIMessage）或 AIMessage 本身，
    两种形态都从其上读 usage_metadata。
    """
    with traced("agent.reason") as span:
        response = handler(request)
        # Token 统计（session 累计 + SSE [METRICS] 推送）+ span 属性；内部静默不影响业务
        record_usage(span, account_response(
            response, fallback_prompt=str(getattr(request, "system_prompt", "") or "")))
        return response

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


# ── 动态工具集（按 query 意图裁剪 tool 列表）──
# 配合 agent/tools/intent_router.py：在每次模型调用前用关键词规则判定 query
# 属于 query/analysis/chat 三档之一，按档裁剪 tools。规则失败默认走全任务
# （analysis 档含 run_full_analysis）—— 解析失败宁可走重路径。
#
# 为什么放在 wrap_model_call 而不是 create_agent 构造时：
# ReAct 单实例一次请求内会多次调用 LLM（tool_call → tool_result → next 决策）；
# 只有第一次调用前需要分类（拿最后一条 user message），后续调用是 ReAct
# 内部循环，request.messages 已经在增长，分类会拿到 tool 调用的中间内容——
# 故意只在"首条 user 消息"时跑一次，靠 runtime.context 缓存避免重复。

# 档位目录见 agent_tools.for_intent（ADR-0004）：每个工具声明固有 min_intent，
# 按秩推导本意图可见工具集。此处只 import 推导入口，不再重复维护工具清单。
from agent.tools.agent_tools import REPORT_MODE, for_intent, mode_effect_for
from agent.tools.intent_router import Intent, classify_with_fallback

# 三档语义：
# - CHAT：纯闲聊/追问；RAG 留作"问业务术语"出口；fill_report_context_for_report
#   保留以确保"生成我的使用报告"旁路不被工具集裁断（它是态切换 hook，@dynamic_prompt
#   report_prompt_switch 依赖 runtime.context[REPORT_MODE]=True，与 tool 集无关）。
# - QUERY：单点数据查询；走 quick_data_insight / get_data_overview / get_chart_insights，
#   故意**不暴露** run_full_analysis —— 这是改造核心目标。
# - ANALYSIS：全集（包含 run_full_analysis），给"分析/出图/报告"显式意图用。
# 以上三档的成员归属在 agent_tools._TOOL_MIN_INTENT 目录表中声明，由 for_intent 推导。


@wrap_model_call
def dynamic_toolset(
        request: ModelRequest,
        handler: Callable[[ModelRequest], object],
):
    """按 query 意图裁剪 ReAct 可见的 tool 列表。

    关键设计：
    1. 只在"首条 user 消息"时分类——靠 runtime.context 缓存意图结果，
       ReAct 循环后续的 model call 直接复用，避免拿到 tool 中间内容做分类。
    2. 分类主路径 = LLM few-shot（classify_with_fallback）；LLM 异常/超时
       回退规则分类；两层都挂才落 ANALYSIS 档（默认走全任务，项目策略）。
    3. ChatTongyi 兼容性：使用 request.override(tools=...) 替换工具集，
       不破坏 LangChain ModelRequest 不可变契约。
    """
    cache = request.runtime.context.setdefault("_intent_cache", {})
    if "intent" in cache:
        intent = cache["intent"]
    else:
        try:
            query = _last_user_content(getattr(request, "messages", None))
            # LLM few-shot 分类为主路径，异常/超时自动回退规则（middleware 永不挂）
            result = classify_with_fallback(query, user_id=get_user_id())
            intent = result.intent
            cache["intent"] = intent
            cache["matched_rule"] = result.matched_rule
            cache["confidence"] = result.confidence
            logger.info(
                f"[dynamic_toolset] intent={intent.value} "
                f"rule={result.matched_rule} confidence={result.confidence} "
                f"query={query[:60]!r}"
            )
        except Exception as e:
            logger.warning(f"[dynamic_toolset] classify failed, fallback to ANALYSIS: {e}")
            intent = Intent.ANALYSIS
            cache["intent"] = intent

    tools = for_intent(intent)
    request = request.override(tools=tools)
    return handler(request)


@dynamic_prompt #每一次在生成prompt之前，调用此函数
def report_prompt_switch(request:ModelRequest):
    return _build_system_prompt(request)


def _build_system_prompt(request: ModelRequest) -> str:
    """构建 system prompt：报告模式用报告 prompt；正常模式追加跨会话召回（ADR-0003 Phase 4）。

    召回并入单条 system prompt（不再另起 system 消息），由本函数统一管控：
    报告模式不注入历史会话记忆（避免泄漏），仅正常模式追加。可独立单测。
    """
    is_report = request.runtime.context.get(REPORT_MODE, False)
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

    runtime.context 每次 agent.stream 重建（见 execute_stream 传 context={REPORT_MODE: False}），
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
