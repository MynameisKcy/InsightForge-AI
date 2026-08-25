"""Token 统计与成本估算（进程级单例，按 session_id 累计）。

挂点（决策 D2，见 docs/specs/2026-08-21-enterprise-observability-plan.md §4.2⑤）：
- agents/base.py `_call_llm`：覆盖全部子 Agent（planner/sql/trend/...）
- agent/agent/tools/middleware.py `trace_model_call`：覆盖 ReactAgent 每次模型调用
session_id 取自 request_context（contextvar，随线程传播）。
RAG chain（返回 str 拿不到 usage）一期未覆盖。

每次累计后经 ProgressEmitter 推送 SSE `[METRICS:{json}]` 事件，前端侧边栏看板消费。
局限：进程内 dict（单副本 uvicorn 够用），重启清零；不落盘。
"""
import contextlib
import os
import threading
from dataclasses import dataclass

try:
    from utils.progress_emitter import get_progress_emitter
    from utils.request_context import get_session_id
except ModuleNotFoundError:
    from agent.utils.progress_emitter import get_progress_emitter
    from agent.utils.request_context import get_session_id

# DashScope 通义千问定价（元/千 token，input/output；随官方调价更新此处即可）
PRICE_TABLE_CNY_PER_K = {
    "qwen-turbo": (0.0003, 0.0006),
    "qwen-plus": (0.0008, 0.002),
    "qwen-max": (0.0024, 0.0096),
}
# 未知模型的兜底价（按 qwen-plus 计）并标注 estimated
_DEFAULT_PRICE = PRICE_TABLE_CNY_PER_K["qwen-plus"]


@dataclass
class TokenUsage:
    """会话累计的 Token 使用统计。"""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_cny: float = 0.0
    calls: int = 0          # LLM 调用次数
    estimated_calls: int = 0  # 按 字符数/4 估算的调用次数（无 usage_metadata）


class TokenCounter:
    """进程级 Token 计数器（线程安全）。"""

    def __init__(self):
        self._session_usage: dict[str, TokenUsage] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _price(model_name: str) -> tuple[float, float]:
        """按模型名查价；env 覆盖 > 价格表 > 默认价。"""
        env_in = os.getenv("TOKEN_PRICE_INPUT")
        env_out = os.getenv("TOKEN_PRICE_OUTPUT")
        if env_in and env_out:
            try:
                return float(env_in), float(env_out)
            except ValueError:
                pass
        return PRICE_TABLE_CNY_PER_K.get(model_name, _DEFAULT_PRICE)

    def record(self, session_id: str, model_name: str,
               input_tokens: int, output_tokens: int, estimated: bool = False) -> TokenUsage | None:
        """记录一次 LLM 调用的 token 用量并返回该会话累计值。

        Args:
            session_id: 会话 ID（空串时仍记录到 "" 桶，不推送 SSE）
            model_name: 模型名（查价格表）
            input_tokens / output_tokens: 本次调用用量
            estimated: True 表示 usage 缺失、按字符数估算
        """
        if input_tokens <= 0 and output_tokens <= 0:
            return None
        with self._lock:
            usage = self._session_usage.setdefault(session_id, TokenUsage())
            usage.input_tokens += input_tokens
            usage.output_tokens += output_tokens
            price_in, price_out = self._price(model_name)
            usage.estimated_cost_cny += (
                input_tokens * price_in + output_tokens * price_out
            ) / 1000.0
            usage.calls += 1
            if estimated:
                usage.estimated_calls += 1
            snapshot = TokenUsage(**usage.__dict__)
        self._emit(session_id, model_name, snapshot, estimated)
        return snapshot

    def record_usage(self, usage: dict | None, model_name: str = "") -> None:
        """记录 LangChain usage_metadata（缺失时跳过——估算场景由调用方显式调 record）。"""
        if not usage:
            return
        self.record(
            get_session_id(), model_name or str(usage.get("model_name", "")),
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
        )

    def record_estimated(self, prompt_text: str, output_text: str, model_name: str = "") -> TokenUsage | None:
        """无 usage_metadata 时的兜底：按 字符数/4 估算 token（中文偏保守）。"""
        return self.record(
            get_session_id(), model_name,
            max(len(prompt_text) // 4, 1), max(len(output_text) // 4, 1),
            estimated=True,
        )

    @staticmethod
    def _emit(session_id: str, model_name: str, usage: TokenUsage, estimated: bool) -> None:
        """推送 SSE [METRICS] 事件（无进度通道/推送失败均静默，不影响业务）。"""
        with contextlib.suppress(Exception):
            emitter = get_progress_emitter()
            if emitter is not None and session_id:
                emitter.emit("metrics", {
                    "session_id": session_id,
                    "model": model_name,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cost_cny": round(usage.estimated_cost_cny, 4),
                    "calls": usage.calls,
                    "estimated": estimated,
                })

    def get_session_usage(self, session_id: str) -> TokenUsage | None:
        """获取会话累计使用量。"""
        with self._lock:
            usage = self._session_usage.get(session_id)
            return TokenUsage(**usage.__dict__) if usage else None

    def clear_session(self, session_id: str) -> None:
        """清除会话统计（登出/删除会话时调用）。"""
        with self._lock:
            self._session_usage.pop(session_id, None)


_counter = None


def get_token_counter() -> TokenCounter:
    global _counter
    if _counter is None:
        _counter = TokenCounter()
    return _counter
