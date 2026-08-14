"""
Agent base class: provides common utilities for all agents.
"""

import json
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from model.factory import get_chat_model


class BaseAgent:
    """所有 Agent 的基类，提供 LLM 调用和 JSON 解析等公共能力。"""

    name: str = "base"

    def __init__(self, user_id=None, model=None):
        # 模型优先用注入（测试/上层注入同一实例）；未注入则按 user_id 解析（factory 缓存）。
        self.model = model if model is not None else get_chat_model(user_id)

    def _call_llm(self, messages: list[dict]) -> str:
        """调用 LLM 并返回文本结果。"""
        from langchain_core.messages import HumanMessage, SystemMessage

        lc_messages = []
        for m in messages:
            if m["role"] == "system":
                lc_messages.append(SystemMessage(content=m["content"]))
            elif m["role"] == "user":
                lc_messages.append(HumanMessage(content=m["content"]))
        response = self.model.invoke(lc_messages)
        return response.content.strip()

    def _parse_json(self, text: str) -> dict:
        """从 LLM 返回的文本中提取 JSON。"""
        text = text.strip()
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 尝试从 markdown code block 中提取
        import re

        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
        # 尝试匹配最外层花括号
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        return {"raw": text, "error": "Failed to parse JSON"}

    def run(self, input_data: dict) -> dict:
        """子类实现具体任务逻辑。"""
        raise NotImplementedError
