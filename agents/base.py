"""
Agent base class: provides common utilities for all agents.
"""

import contextlib
import json

from model.factory import get_chat_model


class BaseAgent:
    """所有 Agent 的基类，提供 LLM 调用和 JSON 解析等公共能力。"""

    name: str = "base"

    def __init__(self, user_id=None, model=None):
        # 模型优先用注入（测试/上层注入同一实例）；未注入则按 user_id 解析（factory 缓存）。
        self.model = model if model is not None else get_chat_model(user_id)

    def _call_llm(self, messages: list[dict]) -> str:
        """调用 LLM 并返回文本结果（带 OTel Span：agent.name + token usage）。"""
        import time

        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            from agent.utils.tracing import get_tracer, record_exception, record_usage
        except ModuleNotFoundError:
            from utils.tracing import get_tracer, record_exception, record_usage

        lc_messages = []
        for m in messages:
            if m["role"] == "system":
                lc_messages.append(SystemMessage(content=m["content"]))
            elif m["role"] == "user":
                lc_messages.append(HumanMessage(content=m["content"]))

        tracer = get_tracer()
        with tracer.start_as_current_span("llm.call") as span:
            span.set_attribute("agent.name", self.name)
            prompt_len = sum(len(m["content"]) for m in messages)
            span.set_attribute("llm.prompt_length", prompt_len)
            start = time.perf_counter()
            try:
                response = self.model.invoke(lc_messages)
                span.set_attribute("duration_ms", round((time.perf_counter() - start) * 1000, 1))
                span.set_attribute("status", "success")
                usage = getattr(response, "usage_metadata", None)
                record_usage(span, usage)
                # Token 统计（session 累计 + SSE [METRICS] 推送）；失败不影响 LLM 调用本身
                with contextlib.suppress(Exception):
                    from utils.token_counter import get_token_counter
                    model_name = (getattr(response, "response_metadata", None) or {}).get("model_name", "")
                    counter = get_token_counter()
                    if usage:
                        counter.record_usage(usage, model_name)
                    else:
                        counter.record_estimated("\n".join(m["content"] for m in messages),
                                                 str(response.content), model_name)
                return response.content.strip()
            except Exception as e:
                record_exception(span, e)
                raise

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
