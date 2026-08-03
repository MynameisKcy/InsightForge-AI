"""
Session Memory（ADR-0003 Phase 1）：单个会话的工作上下文管理。

两级记忆的 Session Memory 层：
- 按 session_id 隔离（每会话一份，不跨会话泄漏）；user_id 仅作归属。
- 池 miss 时从 conversation_history（turn_index > 水印）+ chat_sessions.summary 回灌。
- 滚动摘要 + 水印（summarized_up_to）模型：超过 MAX_TURNS 时把最老若干轮并入滚动摘要，
  推进水印并写回 chat_sessions；全量轮次仍保留在 conversation_history（source of truth）。

水印推进不依赖内存里的 turn_index：DB 中 turn_index 由 save_conversation_pair 按会话连续
分配（0,1,2,...），故新水印 = 旧水印 + 本轮折叠的轮数，内存与 DB 天然同步、无漂移。
"""

import os
import sys
from collections import OrderedDict
from typing import Any

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger

MAX_TURNS = 30  # 触发压缩的轮数阈值（一问一答 = 1 轮）；Phase 2 改为 90% 上下文预算
SESSION_POOL_CAP = 50  # 内存池上限（LRU），超过淘汰最久未用
_KEEP_RECENT = MAX_TURNS // 2  # 压缩后保留的最近轮数（工作窗口缓冲）


class ConversationMemory:
    """Session Memory：单个会话的工作上下文（最近轮次 + 滚动摘要 + 水印）。"""

    def __init__(self, user_id: str = "anonymous", session_id: str = ""):
        self.user_id = user_id
        self.session_id = session_id
        self.turns: list[dict] = []  # 水印之后的最近轮次：[{"role": "user"|"assistant", "content": str}]
        self.summary: str = ""  # 滚动摘要（水印之前所有轮次的压缩）
        self.summarized_up_to: int = -1  # 已并入摘要的最大 turn_index；-1 = 无
        self._hydrated = False  # 是否已从 DB 回灌（懒加载，避免空会话也打 DB）

    # ── 回灌 ──

    def _ensure_hydrated(self):
        """池 miss 后首次访问时从 DB 回灌（懒加载）。

        无 session_id（如退役的 /api/analysis 旧路径）则保持空，不回灌。
        """
        if self._hydrated:
            return
        self._hydrated = True
        if not self.session_id:
            return
        try:
            ltm = _get_ltm()
            meta = ltm.get_session_memory_meta(self.session_id)
            if meta is not None:
                self.summary, self.summarized_up_to = meta
            rows = ltm.get_turns_after(self.session_id, self.summarized_up_to)
            self.turns = [{"role": r["role"], "content": r["content"]} for r in rows]
            logger.debug(
                f"Hydrated session {self.session_id}: {len(self.turns)} msgs, "
                f"summary={'Y' if self.summary else 'N'}, watermark={self.summarized_up_to}"
            )
        except Exception as e:
            logger.warning(f"Hydrate failed for session {self.session_id}: {e}")

    # ── 写入 ──

    def add_user_message(self, content: str):
        self._ensure_hydrated()
        self.turns.append({"role": "user", "content": content})
        self._maybe_compress()

    def add_assistant_message(self, content: str):
        self._ensure_hydrated()
        self.turns.append({"role": "assistant", "content": content})

    # ── 读取 ──

    def get_context(self, max_turns: int = MAX_TURNS) -> list[dict]:
        """返回当前上下文（最近 N 轮）。有滚动摘要则前置摘要 system 消息。"""
        self._ensure_hydrated()
        recent = self.turns[-max_turns * 2:]  # 每轮 = user + assistant = 2 条消息
        if self.summary:
            return [
                {"role": "system", "content": f"[历史对话摘要] {self.summary}"}
            ] + recent
        return recent

    def get_full_history(self) -> list[dict]:
        self._ensure_hydrated()
        return list(self.turns)

    def size(self) -> int:
        """已开始的对话轮数（按 user 消息计数，含当前未配对的）。"""
        return sum(1 for t in self.turns if t["role"] == "user")

    # ── 压缩 ──

    def _maybe_compress(self):
        """超过 MAX_TURNS 时，把最老若干轮并入滚动摘要，推进水印并写回 chat_sessions。

        折叠轮数 = size - _KEEP_RECENT；保留最近 _KEEP_RECENT 轮作工作窗口。
        水印按「旧水印 + 折叠轮数」推进（DB turn_index 连续，无需内存追踪 turn_index）。
        """
        if self.size() <= MAX_TURNS:
            return
        try:
            fold_count = self.size() - _KEEP_RECENT
            if fold_count <= 0:
                return
            fold_turns, keep_turns = self._split_oldest_turns(fold_count)
            if not fold_turns:
                return
            new_summary = _get_summarizer().summarize(fold_turns, self.summary)
            new_watermark = self.summarized_up_to + fold_count
            self.summary = new_summary
            self.summarized_up_to = new_watermark
            self.turns = keep_turns
            logger.info(
                f"Compressed {fold_count} turns for session {self.session_id}, "
                f"watermark -> {new_watermark}"
            )
            try:
                _get_ltm().save_session_memory_meta(
                    self.session_id, new_summary, new_watermark
                )
            except Exception as e:
                logger.warning(f"Failed to persist session memory meta: {e}")
        except Exception as e:
            logger.warning(f"Memory compression failed: {e}")

    def _split_oldest_turns(self, fold_count: int) -> tuple[list[dict], list[dict]]:
        """把最老 fold_count 轮切出来；一轮 = 一条 user 消息 + 其后到下一条 user 前的所有消息。

        保证当前（最后一条、可能未配对的）user 消息始终落在 keep 中，不会被折叠。
        """
        fold, keep = [], []
        seen_users = 0
        folding = True
        for t in self.turns:
            if t["role"] == "user":
                seen_users += 1
                if seen_users > fold_count:
                    folding = False
            (fold if folding else keep).append(t)
        return fold, keep

    # ── 杂项 ──

    def clear(self):
        self.turns = []
        self.summary = ""
        self.summarized_up_to = -1
        self._hydrated = True  # 已清空，无需再回灌

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "turns": self.turns,
            "summary": self.summary,
            "summarized_up_to": self.summarized_up_to,
            "size": self.size(),
        }


# ── 可注入依赖（测试可替换，避免触达真实 LLM / 生产 memory.db）──
# 用整段替换规避 utils/agent.utils 双模块导入陷阱：压缩与回灌只走这两个工厂。
_ltm = None


def _get_ltm():
    """获取 LongTermMemory 单例（懒加载）。"""
    global _ltm
    if _ltm is None:
        from memory.long_term import LongTermMemory
        _ltm = LongTermMemory()
    return _ltm


def _get_summarizer():
    """获取 ConversationSummarizer 实例（无状态，每次新建）。"""
    from memory.summarizer import ConversationSummarizer
    return ConversationSummarizer()


# 全局会话内存池（按 session_id 索引，LRU）
_session_pool: "OrderedDict[str, ConversationMemory]" = OrderedDict()


def get_session(session_id: str, user_id: str = "anonymous") -> ConversationMemory:
    """获取或创建会话级 Session Memory（按 session_id 隔离，LRU 淘汰最久未用）。

    池 miss 时构造空壳 ConversationMemory；首次读取（get_context / add_*）才懒回灌。
    """
    if session_id in _session_pool:
        _session_pool.move_to_end(session_id)  # 访问即提升为最近使用
        return _session_pool[session_id]
    mem = ConversationMemory(user_id=user_id, session_id=session_id)
    _session_pool[session_id] = mem
    while len(_session_pool) > SESSION_POOL_CAP:
        _session_pool.popitem(last=False)  # 淘汰最久未用
    return mem


def clear_session(session_id: str):
    """清除会话级 Session Memory（删会话时调用，释放池内缓存）。"""
    _session_pool.pop(session_id, None)
