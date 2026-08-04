"""
Long-Term Memory 跨会话召回 (ADR-0003 Phase 3)。

Session Memory 解决单会话工作上下文（隔离 + 回灌 + 压缩）；本模块解决跨会话：
- 会话结束（闲置/切换）时把终版摘要写入 memory collection（按 session_id upsert），
  供日后召回；同步留 memory_summaries 行（审计/重建）。
- 每轮聊天可召回该用户相关的历史会话摘要（owner 过滤 + gte-rerank-v2），注入上下文。

终版摘要 = 合并（滚动摘要 + 剩余水印后轮次）压一份。从 DB 取（pool 可能已淘汰），
DB 是 source of truth。闲置检测在请求进来时 piggyback，fire-and-forget 不阻塞当前请求。

finalized_up_to 门控：已 finalize 到最新轮的会话不再重复 LLM 调用；有新轮次则重新
finalize（upsert 覆盖），保证摘要随会话演进刷新。
"""

import os
import sys
from datetime import datetime

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger

# 可调阈值（不影响架构）
MEMORY_RECALL_COARSE_K = 5       # 粗召回候选数（rerank 前）
MEMORY_RECALL_TOP_N = 3          # rerank 后保留数
MEMORY_RERANK_SCORE_THRESHOLD = 0.3  # rerank 分数阈值，低于丢弃
SESSION_IDLE_SECONDS = 1800      # 闲置 30min 视为可 finalize
SECTION_CHAR_CAP = 2000          # 召回节硬限长（字符），约束字符兜底 token 估算偏差


class MemoryRecallService:
    """跨会话记忆召回：终版摘要写入 + 召回 + 闲置检测。

    依赖（ltm / memory_store / summarizer）可经构造参注入，测试绕开真实 DashScope
    与生产库；不传则懒加载生产单例。
    """

    def __init__(self, ltm=None, memory_store=None, summarizer=None):
        self._ltm = ltm
        self._memory_store = memory_store
        self._summarizer = summarizer

    # ── 懒加载依赖（测试可经构造参注入，绕开真实 DashScope/生产库） ──
    @property
    def ltm(self):
        if self._ltm is None:
            from memory.long_term import LongTermMemory
            self._ltm = LongTermMemory()
        return self._ltm

    @property
    def memory_store(self):
        if self._memory_store is None:
            from rag.vector_store import VectorStoreService, MEMORY_COLLECTION
            self._memory_store = VectorStoreService(collection_name=MEMORY_COLLECTION)
        return self._memory_store

    @property
    def summarizer(self):
        if self._summarizer is None:
            from memory.summarizer import ConversationSummarizer
            self._summarizer = ConversationSummarizer()
        return self._summarizer

    # ── 会话结束：写终版摘要 ──
    def finalize_session(self, session_id: str, user_id: str) -> str:
        """合并滚动摘要 + 剩余水印后轮次为终版摘要，upsert 进 memory collection + memory_summaries。

        从 DB 取（pool 可能已淘汰），DB 是 source of truth。无轮次且无滚动摘要则返回空串、不写入。
        重复 finalize 同 session_id 覆盖（delete-then-add）。返回终版摘要文本。
        """
        meta = self.ltm.get_session_memory_meta(session_id)
        if meta is None:
            return ""  # 会话不存在
        rolling_summary, watermark = meta
        remaining = self.ltm.get_turns_after(session_id, watermark)
        # 终版摘要：有剩余轮次则合并压一份；否则沿用滚动摘要
        if remaining:
            try:
                final = self.summarizer.summarize(remaining, rolling_summary)
            except Exception as e:
                logger.warning(f"finalize_session summarize failed: {e}; fallback to rolling summary")
                final = rolling_summary or ""
        else:
            final = rolling_summary or ""
        final = (final or "").strip()
        if not final:
            return ""  # 空会话不写入

        max_idx = self.ltm.get_session_max_turn_index(session_id)
        title = self.ltm.get_session_title(session_id)
        ended_at = datetime.now().isoformat()
        try:
            self.memory_store.add_session_memory(user_id, session_id, final, title, ended_at)
        except Exception as e:
            logger.warning(f"finalize_session add_session_memory failed: {e}")
        try:
            self.ltm.save_summary(user_id, final, turn_count=max_idx + 1, session_id=session_id)
        except Exception as e:
            logger.warning(f"finalize_session save_summary failed: {e}")
        try:
            self.ltm.mark_session_finalized(session_id, max_idx)
        except Exception as e:
            logger.warning(f"finalize_session mark_finalized failed: {e}")
        logger.info(f"Finalized session {session_id} (owner={user_id}, up_to={max_idx})")
        return final

    # ── 召回 ──
    def recall(self, query: str, user_id: str, top_n: int = MEMORY_RECALL_TOP_N,
               coarse_k: int = MEMORY_RECALL_COARSE_K, exclude_session_id: str | None = None) -> str:
        """召回该用户相关的历史会话摘要，返回 "## 历史会话记忆" 节文本；无则空串。

        exclude_session_id 给定时排除当前会话（避免召回自己刚 finalize 的摘要）。
        """
        if not query or not user_id:
            return ""
        try:
            docs = self.memory_store.retrieve_session_memories(
                query, user_id, k=coarse_k, exclude_session_id=exclude_session_id
            )
        except Exception as e:
            logger.warning(f"recall retrieve failed: {e}")
            return ""
        if not docs:
            return ""
        docs = self._rerank(query, docs, top_n)
        return self._format_memory_section(docs)

    def _rerank(self, query: str, docs: list, top_n: int) -> list:
        """gte-rerank-v2 精排；候选 <= top_n 时早退不调 DashScope；失败回退粗召回前 top_n。

        与 rag_service._rerank 同构：gte-rerank 对部分 Key 返回 403，gte-rerank-v2 普遍可用；
        先判 status_code==200 与 output 非空，否则回退。
        """
        if not docs or len(docs) <= top_n:
            return docs[:top_n]
        try:
            from dashscope import TextReRank
            import os as _os
            resp = TextReRank.call(
                model="gte-rerank-v2",
                query=query,
                documents=[d.page_content for d in docs],
                top_n=top_n,
                return_documents=False,
                api_key=_os.getenv("DASHSCOPE_API_KEY"),
            )
            status = getattr(resp, "status_code", None)
            output = getattr(resp, "output", None)
            if status != 200 or not output or not output.get("results"):
                logger.warning(f"memory rerank no valid result (status={status}); fallback coarse")
                return docs[:top_n]
            reranked = []
            for item in output.get("results", []):
                idx = item.get("index")
                score = item.get("relevance_score", 0)
                if idx is None or idx < 0 or idx >= len(docs):
                    continue
                if score < MEMORY_RERANK_SCORE_THRESHOLD:
                    continue
                d = docs[idx]
                d.metadata["rerank_score"] = score
                reranked.append(d)
            return reranked if reranked else docs[:top_n]
        except Exception as e:
            logger.warning(f"memory rerank failed, fallback coarse: {e}")
            return docs[:top_n]

    @staticmethod
    def _format_memory_section(docs: list) -> str:
        """把召回的终版摘要格式化为 "## 历史会话记忆" 节，带标题与日期。

        硬限长 SECTION_CHAR_CAP：召回节不计入 Session Memory 的字符兜底 token 估算
        （实测 input_tokens 路径已含召回，是准的；字符兜底仅首轮/压缩后用、窗口小），
        限长把兜底路径的偏差硬约束，避免召回膨胀撑爆上下文。
        """
        lines = ["## 历史会话记忆"]
        for i, d in enumerate(docs, start=1):
            meta = d.metadata or {}
            title = meta.get("title", "")
            ended_at = meta.get("ended_at", "")
            date_text = ended_at[:10] if ended_at else ""  # YYYY-MM-DD
            head = f"{i}. "
            if title:
                head += f"[{title}"
                if date_text:
                    head += f" | {date_text}"
                head += "] "
            elif date_text:
                head += f"[{date_text}] "
            lines.append(head + d.page_content.strip())
        text = "\n".join(lines)
        if len(text) > SECTION_CHAR_CAP:
            text = text[:SECTION_CHAR_CAP] + "…"
        return text

    def delete_session_memory(self, session_id: str, user_id: str | None = None) -> None:
        """清理某会话的终版摘要 embedding（删会话时调用）。"""
        try:
            self.memory_store.delete_session_memory(session_id, user_id)
        except Exception as e:
            logger.warning(f"delete_session_memory failed: {e}")

    # ── 闲置检测：fire-and-forget ──
    def finalize_idle_sessions(self, user_id: str, except_session_id: str = "",
                               idle_seconds: int = SESSION_IDLE_SECONDS) -> list[str]:
        """finalize 该用户闲置超阈的会话（排除当前会话）。

        finalized_up_to 门控：已 finalize 到最新轮的会话跳过，避免重复 LLM 调用；
        有新轮次（max turn_index > finalized_up_to）则重新 finalize（upsert 覆盖）。
        返回已 finalize 的 session_id 列表。设计为在后台线程跑，不阻塞当前请求。
        """
        finalized: list[str] = []
        try:
            sessions = self.ltm.get_idle_sessions(user_id, except_session_id, idle_seconds)
        except Exception as e:
            logger.warning(f"finalize_idle_sessions get_idle failed: {e}")
            return finalized
        for s in sessions:
            sid = s.get("session_id")
            if not sid:
                continue
            max_idx = self.ltm.get_session_max_turn_index(sid)
            if max_idx < 0:
                continue  # 无轮次，无可摘要
            finalized_up_to = int(s.get("finalized_up_to") if s.get("finalized_up_to") is not None else -1)
            if finalized_up_to >= max_idx:
                continue  # 已覆盖到最新，不重复
            try:
                self.finalize_session(sid, user_id)
                finalized.append(sid)
            except Exception as e:
                logger.warning(f"finalize idle session {sid} failed: {e}")
        if finalized:
            logger.info(f"Finalized {len(finalized)} idle session(s) for user {user_id}: {finalized}")
        return finalized


# 模块级单例（供 fastapi / 其他模块复用）
_recall_service = None


def get_memory_recall() -> MemoryRecallService:
    global _recall_service
    if _recall_service is None:
        _recall_service = MemoryRecallService()
    return _recall_service


def reset_memory_recall() -> None:
    """测试用：重置模块级单例。"""
    global _recall_service
    _recall_service = None
