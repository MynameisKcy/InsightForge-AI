"""
Session Memory（ADR-0003）：单个会话的工作上下文管理。

两级记忆的 Session Memory 层：
- 按 session_id 隔离（每会话一份，不跨会话泄漏）；user_id 仅作归属。
- 池 miss 时从 conversation_history（turn_index > 水印）+ chat_sessions.summary 回灌。
- 滚动摘要 + 水印（summarized_up_to）模型：上下文用量达阈值时把最老若干轮并入滚动摘要，
  推进水印并写回 chat_sessions；全量轮次仍保留在 conversation_history（source of truth）。

压缩触发（Phase 2）：以最近一次模型调用实测的 input_tokens 为信号（ChatTongyi 在
response_metadata['token_usage']['input_tokens'] 提供，已 spike 验证），>= 90% 上下文窗口触发；
首轮/压缩后无实测时用字符启发式兜底，>= 80% 触发。折叠折半（至少保留 min_keep_turns 轮）。
反应式溢出重试暂缓（主动用模型自身测量已较准；溢出异常类型未定 + 流式重启去重风险）。

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
from memory.context_budget import (
    compress_threshold,
    char_fallback_threshold,
    min_keep_turns,
    estimate_messages_tokens,
)

MAX_TURNS = 30  # get_context 默认截断的轮数（一问一答 = 1 轮）；聊天路径传 None 不截断
SESSION_POOL_CAP = 50  # 内存池上限（LRU），超过淘汰最久未用


class ConversationMemory:
    """Session Memory：单个会话的工作上下文（最近轮次 + 滚动摘要 + 水印）。"""

    def __init__(self, user_id: str = "anonymous", session_id: str = ""):
        self.user_id = user_id
        self.session_id = session_id
        self.turns: list[dict] = []  # 水印之后的最近轮次：[{"role": "user"|"assistant", "content": str}]
        self.summary: str = ""  # 滚动摘要（水印之前所有轮次的压缩）
        self.summarized_up_to: int = -1  # 已并入摘要的最大 turn_index；-1 = 无
        self._hydrated = False  # 是否已从 DB 回灌（懒加载，避免空会话也打 DB）
        self.last_measured_input_tokens: int | None = None  # 最近一次模型调用实测的输入 token 数
        self._context_window: int | None = None  # 缓存的上下文窗口；None -> 懒解析

    @property
    def context_window(self) -> int:
        """该会话所用模型的上下文窗口（最大输入 token 数）；测试可直接覆写 _context_window。"""
        if self._context_window is None:
            try:
                from memory.context_budget import get_context_window
                self._context_window = get_context_window(self.user_id)
            except Exception:
                self._context_window = 32768
        return self._context_window

    def record_input_tokens(self, n: int | None):
        """记录最近一次模型调用实测的输入 token 数（供下一轮主动压缩判定）。None 不覆盖。"""
        if n is not None and n >= 0:
            self.last_measured_input_tokens = int(n)

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

    def get_context(self, max_turns: int | None = MAX_TURNS) -> list[dict]:
        """返回当前上下文。有滚动摘要则前置摘要 system 消息。

        max_turns=None 时不截断（聊天路径依赖 token 预算压缩,非轮数帽）；默认 MAX_TURNS 截断
        （bridge 共指改写只需最近几轮,传 6）。
        """
        self._ensure_hydrated()
        recent = self.turns if max_turns is None else self.turns[-max_turns * 2:]
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
        """上下文用量达阈值时压缩：实测 input_tokens >= 90% 或字符兜底 >= 80%。

        折叠最老的「半数」轮次（近似压到 ~50%），至少保留 min_keep_turns 轮；
        水印按「旧水印 + 折叠轮数」推进（DB turn_index 连续，无漂移）；摘要 + 水印写回
        chat_sessions。压缩后实测值失效（下一轮退回字符兜底，直至新一轮模型调用回填）。
        """
        est, threshold = self._estimate_and_threshold()
        if est < threshold * self.context_window:
            return
        try:
            size = self.size()
            keep = min_keep_turns()
            if size <= keep:
                return  # 轮次太少无可折叠（单条超大消息的溢出由反应式重试兜底，暂缓）
            # 折半折叠，但至少保留 keep 轮
            fold_count = size // 2
            fold_count = max(1, min(fold_count, size - keep))
            fold_turns, keep_turns = self._split_oldest_turns(fold_count)
            if not fold_turns:
                return
            new_summary = _get_summarizer().summarize(fold_turns, self.summary)
            new_watermark = self.summarized_up_to + fold_count
            self.summary = new_summary
            self.summarized_up_to = new_watermark
            self.turns = keep_turns
            self.last_measured_input_tokens = None  # 失效：下轮用字符兜底
            logger.info(
                f"Compressed {fold_count} turns for session {self.session_id} "
                f"(est~{est}/{self.context_window}), watermark -> {new_watermark}"
            )
            try:
                _get_ltm().save_session_memory_meta(
                    self.session_id, new_summary, new_watermark
                )
            except Exception as e:
                logger.warning(f"Failed to persist session memory meta: {e}")
        except Exception as e:
            logger.warning(f"Memory compression failed: {e}")

    def _estimate_and_threshold(self) -> tuple[int, float]:
        """返回 (当前 prompt 估算 token, 触发阈值比例)。

        有实测 input_tokens 时：它反映上一轮 prompt，本轮新增了「上一轮助手回复 + 当前用户
        消息」（self.turns[-2:]），叠加后更贴近当前 prompt（防止单轮激增时触发滞后）。90% 阈值。
        否则字符启发式兜底（summary + 全部 turns），80% 阈值，更保守。
        """
        if self.last_measured_input_tokens is not None:
            delta = estimate_messages_tokens("", self.turns[-2:]) if len(self.turns) >= 2 else 0
            return self.last_measured_input_tokens + delta, compress_threshold()
        return estimate_messages_tokens(self.summary, self.turns), char_fallback_threshold()

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
        self.last_measured_input_tokens = None
        self._hydrated = True  # 已清空，无需再回灌

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "turns": self.turns,
            "summary": self.summary,
            "summarized_up_to": self.summarized_up_to,
            "last_measured_input_tokens": self.last_measured_input_tokens,
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


_summarizer_factory = None


def set_summarizer_factory(factory):
    """设置 summarizer 工厂（由 MemoryService 在初始化时调用）。

    打破 memory → agents 的循环依赖：llm_callable 由上层注入，
    ConversationSummarizer 不再自行导入 BaseAgent。
    """
    global _summarizer_factory
    _summarizer_factory = factory


def _get_summarizer():
    """获取 ConversationSummarizer 实例（无状态，每次新建）。"""
    if _summarizer_factory:
        return _summarizer_factory()
    from memory.summarizer import ConversationSummarizer
    return ConversationSummarizer(lambda msgs: "")


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
