"""
Conversation Summarizer: 使用 LLM 将多轮对话压缩为简短摘要。
"""

import os
import sys
from typing import Callable

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger


SUMMARY_PROMPT = """你是一个对话摘要生成器。请将以下对话历史压缩为一段简洁的摘要（不超过 300 字）。

## 要求
1. 保留关键的数据分析查询和结论。
2. 保留用户关心的问题和重要发现。
3. 保留重要的数字和趋势。
4. 忽略寒暄和无关内容。
5. 如果有之前的摘要，将其与新对话合并成一个连贯的摘要。

## 之前的摘要
{previous_summary}

## 新对话历史
{conversation}

## 输出
直接输出摘要文本，不要加任何前缀或格式。
"""


class ConversationSummarizer:
    """使用 LLM 将对话压缩为摘要。

    llm_callable 由调用方注入，接受 messages: list[dict] 返回 str。
    打破了对 agents.base.BaseAgent 的循环依赖（memory → agents → memory）。
    """

    def __init__(self, llm_callable: Callable[[list[dict]], str]):
        self._call_llm = llm_callable

    def summarize(self, turns: list[dict], previous_summary: str = "") -> str:
        """将 turns 列表压缩为摘要文本。"""
        # 格式化对话
        conversation_text = self._format_turns(turns)
        if not conversation_text.strip():
            return previous_summary or "无对话内容"

        prompt = SUMMARY_PROMPT.format(
            previous_summary=previous_summary or "无",
            conversation=conversation_text[:3000],  # 限制长度
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            summary = self._call_llm(messages)
            return summary.strip()[:500]  # 限制摘要长度
        except Exception as e:
            logger.warning(f"Conversation summarization failed: {e}")
            # 降级：简单截取
            return self._fallback_summary(turns)

    def _format_turns(self, turns: list[dict]) -> str:
        """格式化对话轮次。"""
        lines = []
        for t in turns:
            role = "用户" if t.get("role") == "user" else "助手"
            content = str(t.get("content", ""))[:200]  # 每条截取 200 字
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _fallback_summary(self, turns: list[dict]) -> str:
        """LLM 不可用时的降级摘要。"""
        user_msgs = [t.get("content", "") for t in turns if t.get("role") == "user"]
        topics = set()
        for msg in user_msgs[:10]:
            topics.add(str(msg)[:50])
        return "对话涉及主题: " + "; ".join(topics)[:300]
