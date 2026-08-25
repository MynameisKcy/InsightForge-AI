"""api 层共享服务接缝：懒加载单例 + per-user 工厂。

路由模块统一以 ``deps._get_xxx()`` 在请求期动态解析（而非 from-import 固定绑定），
使测试可对 ``api.deps`` 模块属性换桩（conftest.swap_srv_seam）。
"""

# ── MemoryService（懒加载单例；llm 按用户解析，由首次请求触发）──
_memory_service = None


def _get_memory_service(user_id: str = "default"):
    """获取 MemoryService 单例（懒加载；summarizer 经 llm_factory(user_id) 按用户解析模型）。

    user_id 仅供未来扩展（当前单例不持有用户状态）；按用户解析发生在
    summarizer 调用时——工厂收到调用方的 user_id，后台闲置 finalize 线程
    与请求线程互不干扰（消除了旧 _memory_llm_user 共享字典的竞态）。
    """
    global _memory_service
    if _memory_service is None:
        from memory.service import MemoryService
        from model.factory import get_chat_model

        def _llm_factory(uid: str):
            def _llm_call(messages: list[dict]) -> str:
                from langchain_core.messages import HumanMessage
                llm = get_chat_model(uid)
                return llm.invoke(
                    [HumanMessage(content=m["content"]) for m in messages]
                ).content

            return _llm_call

        _memory_service = MemoryService(_llm_factory)
    return _memory_service


def begin_memory_turn(user_id: str, session_id: str = "", query: str = ""):
    """begin_turn 括号入口收口：返回 (turn, err)。

    会话不存在/无权时 turn 为 None、err 为错误文案（路由统一回 404）；
    成功时 err 为空串。chat/analysis 两路由各自的 try/except PermissionError
    由此收敛为一处。
    """
    try:
        return _get_memory_service(user_id).begin_turn(user_id, session_id, query), ""
    except PermissionError as e:
        return None, str(e)


# ── Agent 实例（按 user_id 隔离：各用户独立实例，用自己的 LLM 配置） ──
# 旧设计是进程级单例，导致 per-user LLM 配置要么失效（单例已建）要么泄漏（首次构建用
# 某用户配置服务所有人）。改为按 user_id 缓存独立实例，配置变更时丢弃对应用户实例。
_react_agents = {}    # user_id -> ReactAgent
# PlannerAgent 的 per-user 缓存由 agent_tools 统一持有（run_full_analysis 工具的入口），
# 见 agent_tools._get_or_create_analyst；此处不再重复缓存，避免两份实例需要分别失效。


def _get_react_agent(user_id: str):
    if user_id not in _react_agents:
        from agent.react_agent import ReactAgent
        _react_agents[user_id] = ReactAgent(user_id=user_id)
    return _react_agents[user_id]


def _get_planner_agent(user_id: str):
    """委托给 agent_tools 的 per-user PlannerAgent 缓存（单一真相源）。

    /api/analysis（ADR-0001 已弃用，前端不再调用）与 run_full_analysis 工具共用同一份
    per-user 实例缓存，配置变更时只需失效一处（见 _invalidate_user_agents）。
    """
    from agent.tools.agent_tools import _get_or_create_analyst
    return _get_or_create_analyst(user_id)


def _invalidate_user_agents(user_id: str):
    """配置保存后调用：丢弃该用户的 Agent 实例，下次请求按新配置重建。
    配合 factory.reload_model_config(user_id) 一并清模型缓存。"""
    _react_agents.pop(user_id, None)
    # PlannerAgent 实例缓存由 agent_tools 持有，统一在此失效
    from agent.tools.agent_tools import invalidate_analyst
    invalidate_analyst(user_id)


# ── 知识库向量库服务（单例） ──
_vector_store_service = None


def _get_vector_store():
    """延迟初始化向量库服务（方案C：运行时知识库管理）。"""
    global _vector_store_service
    if _vector_store_service is None:
        from rag.vector_store import VectorStoreService
        _vector_store_service = VectorStoreService()
    return _vector_store_service
