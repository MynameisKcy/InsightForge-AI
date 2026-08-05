"""
MemoryService 外观：编排 Session Memory + Long-Term Memory + Recall 的完整生命周期。

将原本分散在 fastapi_server、react_agent、planner_agent、middleware 四个调用方
的内存操作统一为两个方法：begin_turn() / end_turn()。调用方不再需要了解
get_session()、_long_term_memory、get_memory_recall()、record_input_tokens()
等内部细节。

循环依赖消除：llm_callable 由上层注入，ConversationSummarizer 不再自行导入
BaseAgent（memory → agents → memory 循环已打破）。
"""

import threading
from dataclasses import dataclass
from typing import Callable, Optional

from memory.long_term import LongTermMemory
from memory.short_term import get_session, set_summarizer_factory
from memory.recall import get_memory_recall, MemoryRecallService
from memory.summarizer import ConversationSummarizer
from utils.logger_handler import logger


@dataclass
class MemoryTurnContext:
    """begin_turn() 返回的上下文，供调用方使用。"""

    session_id: str
    """已解析（或新创建）的会话 ID。"""

    mem_context: list[dict]
    """Session Memory 的历史上下文（消息列表），可直接传给 LLM。"""

    is_new_session: bool
    """是否为本次请求新创建的会话。"""


class MemoryService:
    """统一内存外观 —— 编排 Session Memory + Long-Term Memory + Recall。

    使用方式:
        svc = MemoryService(llm_callable)
        turn = svc.begin_turn(user_id, session_id, query)
        # ... agent 处理 ...
        svc.end_turn(user_id, turn.session_id, query, assistant_response,
                     input_tokens=n)
    """

    def __init__(self, llm_callable: Callable[[list[dict]], str]):
        """
        Args:
            llm_callable: LLM 调用函数，接受 messages: list[dict]，返回 str。
                          用于 ConversationSummarizer。由上层（factory.get_chat_model）
                          创建，打破 memory → agents 的循环依赖。
        """
        self._llm_callable = llm_callable
        self._ltm = LongTermMemory()
        self._recall = get_memory_recall()

        # 注入 summarizer 工厂，使 short_term 和 recall 模块的懒加载
        # ConversationSummarizer 使用上层注入的 llm_callable，不再自行导入 BaseAgent。
        set_summarizer_factory(lambda: ConversationSummarizer(llm_callable))

    @property
    def ltm(self) -> LongTermMemory:
        """暴露 LongTermMemory 供非 chat 端点使用（会话列表、历史、删除等）。"""
        return self._ltm

    # ── 公开接口 ──

    def begin_turn(
        self,
        user_id: str,
        session_id: str = "",
        query: str = "",
    ) -> MemoryTurnContext:
        """请求开始时调用。

        内部处理：会话创建/解析 → IDOR 校验 → touch → piggyback 闲置 finalize →
        Session Memory 水合 → 上下文检索 → 添加用户消息。

        Args:
            user_id: 当前用户 ID。
            session_id: 会话 ID，空字符串表示创建新会话。
            query: 用户当前消息文本。

        Returns:
            MemoryTurnContext: session_id、mem_context、is_new_session。
        """
        is_new_session = False

        # 1. 会话创建/解析 + IDOR
        if not session_id:
            title = query[:30] + ("..." if len(query) > 30 else "")
            session_id = self._ltm.create_session(user_id, title=title)
            is_new_session = True
        else:
            owner = self._ltm.get_session_owner(session_id)
            if owner is None or owner != user_id:
                raise PermissionError("会话不存在或无权访问")
            self._ltm.touch_session(session_id)

        # 2. piggyback 闲置会话 finalize（fire-and-forget，不阻塞当前请求）
        self._trigger_idle_finalize(user_id, session_id)

        # 3. Session Memory：按 session_id 隔离 + 池 miss 时从 DB 回灌
        memory = get_session(session_id, user_id)
        # 获取历史上下文（在 add_user_message 之前，避免当前消息重复）
        mem_context = memory.get_context(max_turns=None)
        memory.add_user_message(query)

        return MemoryTurnContext(
            session_id=session_id,
            mem_context=mem_context,
            is_new_session=is_new_session,
        )

    def end_turn(
        self,
        user_id: str,
        session_id: str,
        query: str,
        assistant_response: str,
        input_tokens: Optional[int] = None,
    ) -> None:
        """请求结束时调用。

        内部处理：添加助手消息 → 持久化到 conversation_history →
        记录 token 使用量（触发压缩检查）。

        Args:
            user_id: 当前用户 ID。
            session_id: 会话 ID。
            query: 用户原始消息。
            assistant_response: 助手完整回复。
            input_tokens: LLM 输入 token 数（可选，用于压缩检查）。
        """
        if not assistant_response.strip():
            return

        memory = get_session(session_id, user_id)
        memory.add_assistant_message(assistant_response.strip())

        try:
            self._ltm.save_conversation_pair(
                user_id, query, assistant_response.strip(), session_id=session_id
            )
        except Exception as e:
            logger.warning(f"Failed to save conversation to long-term memory: {e}")

        if input_tokens is not None:
            try:
                memory.record_input_tokens(input_tokens)
            except Exception as e:
                logger.warning(f"Failed to record input tokens: {e}")

    # ── 内部方法 ──

    def _trigger_idle_finalize(self, user_id: str, except_session_id: str) -> None:
        """触发闲置会话终版摘要写入（fire-and-forget 后台线程）。"""
        try:
            threading.Thread(
                target=self._recall.finalize_idle_sessions,
                args=(user_id,),
                kwargs={"except_session_id": except_session_id},
                daemon=True,
            ).start()
        except Exception as e:
            logger.warning(f"finalize_idle_sessions trigger failed: {e}")