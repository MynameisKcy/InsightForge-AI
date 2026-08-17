"""
MemoryService 外观：编排 Session Memory + Long-Term Memory + Recall 的完整生命周期。

将原本分散在 fastapi_server、react_agent、planner_agent、middleware 四个调用方
的内存操作统一为两个方法：begin_turn() / end_turn()。调用方不再需要了解
get_session()、_long_term_memory、get_memory_recall()、record_input_tokens()
等内部细节。

循环依赖消除：llm 由上层注入的工厂按用户解析，ConversationSummarizer 不再自行导入
BaseAgent（memory → agents → memory 循环已打破）。工厂接受 user_id——后台闲置
finalize 线程与请求线程各自解析正确用户的模型，无共享"当前用户"状态。
"""

import threading
from dataclasses import dataclass
from typing import Callable, Optional

from memory.long_term import LongTermMemory
from memory.short_term import clear_session, get_session, set_summarizer_factory
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
        svc = MemoryService(llm_factory)
        turn = svc.begin_turn(user_id, session_id, query)
        # ... agent 处理 ...
        svc.end_turn(user_id, turn.session_id, query, assistant_response,
                     input_tokens=n)
    """

    def __init__(self, llm_factory: Callable[[str], Callable[[list[dict]], str]]):
        """
        Args:
            llm_factory: user_id -> llm_callable 工厂；llm_callable 接受
                          messages: list[dict] 返回 str，用于 ConversationSummarizer。
                          按用户解析模型（网页设置 > .env），由上层
                          （factory.get_chat_model）创建，打破 memory → agents 的循环依赖。
        """
        self._llm_factory = llm_factory
        self._ltm = LongTermMemory()
        self._recall = get_memory_recall()

        # 注入按用户的 summarizer 工厂，使 short_term 和 recall 模块的懒加载
        # ConversationSummarizer 用正确用户的 llm_callable，不再自行导入 BaseAgent。
        set_summarizer_factory(
            lambda uid: ConversationSummarizer(llm_factory(uid))
        )

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

    # ── 会话生命周期（admin / read）── 拉到外观上 ──

    def list_sessions(self, user_id: str) -> list[dict]:
        """返回用户的所有会话列表（按最近活跃排序）。"""
        return self._ltm.get_user_sessions(user_id)

    def get_conversation_history(self, user_id: str, limit: int = 20) -> list[dict]:
        """返回用户级跨会话最近 N 轮（遗留兼容端点用）。"""
        return self._ltm.get_last_n_turns(user_id, n=limit)

    def get_session(self, user_id: str, session_id: str) -> list[dict]:
        """返回指定会话的完整对话历史；不存在或不属于该用户则 raise PermissionError。"""
        self._assert_owner(session_id, user_id)
        return self._ltm.get_session_conversation(session_id)

    def rename_session(self, user_id: str, session_id: str, title: str) -> None:
        """重命名会话标题；不存在或不属于该用户则 raise PermissionError。"""
        self._assert_owner(session_id, user_id)
        self._ltm.update_session_title(session_id, title)

    def delete_session(self, user_id: str, session_id: str) -> None:
        """删除会话及其全部记忆（跨 3 层）。

        IDOR 校验先于任何删除；通过后依次：
        LTM 删除会话 → short_term 释放池内 Session Memory → recall 清理跨会话 embedding。
        recall 失败仅记日志，不回滚（与现状一致）。
        """
        self._assert_owner(session_id, user_id)
        self._ltm.delete_session(session_id)
        clear_session(session_id)  # 释放池内 Session Memory 缓存（ADR-0003）
        try:
            self._recall.delete_session_memory(session_id, user_id)
        except Exception as e:
            logger.warning(f"delete session memory embedding failed: {e}")

    # ── 内部方法 ──

    def _assert_owner(self, session_id: str, user_id: str) -> None:
        """IDOR 归属校验的唯一归处：会话不存在或不属于该用户一律拒绝。

        与 begin_turn 一致，raise PermissionError；调用方据此返回 404
        （"不存在"与"无权"统一 404，避免会话 ID 枚举）。
        """
        owner = self._ltm.get_session_owner(session_id)
        if owner is None or owner != user_id:
            raise PermissionError("会话不存在或无权访问")

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