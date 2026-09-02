"""
Agent base class: provides common utilities for all agents.
"""

import json

from agents.schemas import validate
from model.factory import get_chat_model
from utils.token_counter import account_response
from utils.tracing import record_usage, traced


class BaseAgent:
    """所有 Agent 的基类，提供 LLM 调用和 JSON 解析等公共能力。"""

    name: str = "base"

    def __init__(self, user_id=None, model=None):
        # 模型优先用注入（测试/上层注入同一实例）；未注入则按 user_id 解析（factory 缓存）。
        self.model = model if model is not None else get_chat_model(user_id)

    def _call_llm(self, messages: list[dict]) -> str:
        """调用 LLM 并返回文本结果（带 OTel Span：agent.name + token usage）。"""
        from langchain_core.messages import HumanMessage, SystemMessage

        lc_messages = []
        for m in messages:
            if m["role"] == "system":
                lc_messages.append(SystemMessage(content=m["content"]))
            elif m["role"] == "user":
                lc_messages.append(HumanMessage(content=m["content"]))

        with traced("llm.call", attrs={"agent.name": self.name}) as span:
            span.set_attribute("llm.prompt_length",
                               sum(len(m["content"]) for m in messages))
            response = self.model.invoke(lc_messages)
            # Token 统计（session 累计 + SSE [METRICS] 推送）+ span 属性；内部静默不影响业务
            record_usage(span, account_response(
                response, fallback_prompt="\n".join(m["content"] for m in messages)))
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

    def _call_llm_with_schema(self, messages: list[dict], schema: dict,
                              retries: int = 1) -> dict | None:
        """调用 LLM 并按 schema 做结构校验，不过则携带具体校验错误重试。

        重试提示词带上前次的具体错误（如「depends_on 应为整数列表」），
        比裸说「请严格按 JSON」修得准。重试耗尽返回 None，降级策略由
        调用方决定（planner → _default_plan；analysis → apply_insight_fallback）。
        空 schema（{}）= 任意 dict 通过（未声明契约的适配器宽进）。
        """
        text = self._call_llm(messages)
        parsed = self._parse_json(text)
        errs = validate(parsed, schema)
        if not errs:
            return parsed
        for _ in range(max(0, retries)):
            messages = messages + [{
                "role": "user",
                "content": ("上次输出未通过结构校验："
                            + "；".join(errs[:3])
                            + "。请修正后严格按 JSON 格式重新输出完整结果，不要包含任何其他文本。"),
            }]
            text = self._call_llm(messages)
            parsed = self._parse_json(text)
            errs = validate(parsed, schema)
            if not errs:
                return parsed
        return None

    def run(self, input_data: dict) -> dict:
        """子类实现具体任务逻辑。"""
        raise NotImplementedError


def parse_json_list(text: str) -> list:
    """从 LLM 输出中提取 JSON 数组。

    _parse_json 只覆盖对象（花括号提取），数组场景（如图表规格列表）需要
    独立实现：qwen3.7 等模型会把 JSON 数组包在 ```json 围栏里输出，
    直接 json.loads 失败后须依次尝试围栏剥离与方括号提取。
    解析失败或结果不是 list 时返回 []。
    """
    import re

    text = (text or "").strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue
    return []
