"""模型上下文预算与 token 估算（ADR-0003 Phase 2）。

ChatTongyi 不回填标准 `usage_metadata`，但在最终 AIMessage 的
`response_metadata['token_usage']['input_tokens']` 提供真实输入 token 数
（已 spike 验证：`create_agent` + `stream_mode="values"` 同样保留）。

压缩触发策略：
- 主动：以最近一次模型调用实测的 input_tokens 为信号（最准），>= compress_threshold(90%) 触发。
- 兜底：首轮/压缩后无实测时用字符启发式估算，>= char_fallback_threshold(80%) 触发（更保守）。
- 反应式重试（API 溢出错误 -> 压一轮 -> 重试）暂缓：主动用模型自身测量已较准，且 DashScope
  溢出异常类型未确定、流式重启有去重风险；待观测到真实溢出再补。
"""

import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.config_handler import model_context_conf

# 配置缺失时的兜底默认（与 model_context.yml 对齐）
_DEFAULT_CONTEXT_WINDOW = 32768
_COMPRESS_THRESHOLD = 0.9
_CHAR_FALLBACK_THRESHOLD = 0.8
_CHARS_PER_TOKEN = 2.5
_MIN_KEEP_TURNS = 3


def _conf(key: str, default):
    v = model_context_conf.get(key) if model_context_conf else None
    return v if v is not None else default


def get_context_window_for_name(model_name: str) -> int:
    """按模型名查上下文窗口（最大输入 token 数）；未列出走 default。"""
    conf = model_context_conf or {}
    window = conf.get(model_name)
    if window is None:
        window = conf.get("default")
    return int(window or _DEFAULT_CONTEXT_WINDOW)


def get_context_window(user_id: str) -> int:
    """解析该 user 当前模型的上下文窗口（最大输入 token 数）。

    模型名经 factory.get_chat_model_name(user_id) 解析（用户配置 > .env > YAML）。
    """
    try:
        from model.factory import get_chat_model_name
        name = get_chat_model_name(user_id)
    except Exception:
        name = ""
    return get_context_window_for_name(name)


def compress_threshold() -> float:
    """实测 token 路径的触发比例（默认 0.9）。"""
    return float(_conf("compress_threshold", _COMPRESS_THRESHOLD))


def char_fallback_threshold() -> float:
    """字符启发式兜底的触发比例（默认 0.8，更保守）。"""
    return float(_conf("char_fallback_threshold", _CHAR_FALLBACK_THRESHOLD))


def min_keep_turns() -> int:
    """压缩后至少保留的最近轮数（默认 3）。"""
    return int(_conf("min_keep_turns", _MIN_KEEP_TURNS))


def estimate_messages_tokens(summary: str, turns: list[dict]) -> int:
    """字符启发式估算 prompt token 数（无实测时的兜底）。

    summary + 各轮 content 的总字符数 / chars_per_token。中文偏保守（1 token ≈ 2.5 字符）。
    """
    total_chars = len(summary or "")
    for t in turns:
        total_chars += len(t.get("content", "") or "")
    return int(total_chars / float(_conf("chars_per_token", _CHARS_PER_TOKEN)))


def extract_input_tokens(message) -> int | None:
    """从 AIMessage 的 response_metadata 抽 input_tokens；无则 None。

    ChatTongyi 流式最终 chunk 的 response_metadata['token_usage']['input_tokens']。
    """
    try:
        rm = getattr(message, "response_metadata", None) or {}
        tu = rm.get("token_usage") or {}
        it = tu.get("input_tokens")
        return int(it) if it is not None else None
    except Exception:
        return None
