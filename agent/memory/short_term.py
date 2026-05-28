"""
Short-Term Memory: 当前对话上下文管理，最多保留 30 轮对话。
超过 30 轮时自动将前 30 轮压缩为摘要，存入长期记忆。
"""

import json
import os
import sys
from typing import Any

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger

MAX_TURNS = 30  # 最大对话轮数（一问一答 = 1 轮）


class ConversationMemory:
    """短期记忆：存储当前会话的对话历史。"""

    def __init__(self, user_id: str = "anonymous"):
        self.user_id = user_id
        self.turns: list[dict] = []  # [{"role": "user"|"assistant", "content": str}]
        self.summary: str = ""  # 超出 30 轮后生成的摘要

    def add_user_message(self, content: str):
        self.turns.append({"role": "user", "content": content})
        self._maybe_compress()

    def add_assistant_message(self, content: str):
        self.turns.append({"role": "assistant", "content": content})

    def get_context(self, max_turns: int = MAX_TURNS) -> list[dict]:
        """返回当前上下文（最近 N 轮）。如果有摘要，前面加上摘要信息。"""
        recent = self.turns[-max_turns * 2:]  # 每轮 = user + assistant = 2 条消息
        if self.summary:
            summary_msg = {
                "role": "system",
                "content": f"[历史对话摘要] {self.summary}",
            }
            return [summary_msg] + recent
        return recent

    def get_full_history(self) -> list[dict]:
        return list(self.turns)

    def size(self) -> int:
        return len(self.turns) // 2  # 按轮计算

    def _maybe_compress(self):
        """当对话超过 30 轮时，压缩前 30 轮。"""
        if self.size() <= MAX_TURNS:
            return
        try:
            from memory.summarizer import ConversationSummarizer
            summarizer = ConversationSummarizer()
            old_turns = self.turns[:MAX_TURNS * 2]  # 前 30 轮
            new_summary = summarizer.summarize(old_turns, self.summary)
            self.summary = new_summary
            # 保留后面的对话
            self.turns = self.turns[MAX_TURNS * 2:]
            logger.info(f"Compressed {MAX_TURNS} turns for user {self.user_id}")

            # 异步写入长期记忆
            try:
                from memory.long_term import LongTermMemory
                ltm = LongTermMemory()
                ltm.save_summary(self.user_id, new_summary, len(old_turns))
            except Exception as e:
                logger.warning(f"Failed to save summary to long-term memory: {e}")
        except Exception as e:
            logger.warning(f"Memory compression failed: {e}")

    def clear(self):
        self.turns = []
        self.summary = ""

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "turns": self.turns,
            "summary": self.summary,
            "size": self.size(),
        }


# 全局会话内存池（按 user_id 索引）
_session_pool: dict[str, ConversationMemory] = {}


def get_session(user_id: str) -> ConversationMemory:
    """获取或创建用户会话。"""
    if user_id not in _session_pool:
        _session_pool[user_id] = ConversationMemory(user_id)
    return _session_pool[user_id]


def clear_session(user_id: str):
    """清除用户会话。"""
    if user_id in _session_pool:
        _session_pool[user_id].clear()
        del _session_pool[user_id]
