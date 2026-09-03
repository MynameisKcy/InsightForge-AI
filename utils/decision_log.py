"""Agent 决策结构化日志：JSONL 落盘 logs/decisions/，供前端展示与事后调试。

数据源（见 docs/specs/2026-08-21-enterprise-observability-plan.md §4.2）：
- monitor_tool：每次工具调用的 tool/args/耗时/结果摘要
- ReactAgent：被内部独白过滤器拦截的 LLM 推理文本
- PlannerAgent：规划理由（随 plan 进度事件下发前端，此处一并落盘）

文件按 日期_用户 分片（logs/decisions/2026-09-10_u_xxx.jsonl），多用户不混写。
写入失败静默——决策日志是旁路能力，不允许影响主流程。

SSE 解耦：本模块不感知传输层——组合根（api/fastapi_server.py）经
set_decision_publisher 注入发布器（progress_emitter.emitter_bridge("decision")）；
未接线时 [DECISION] 事件静默丢弃，JSONL 落盘不受影响。
"""
import contextlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from utils.path_tool import get_abs_path
from utils.request_context import get_session_id, get_user_id

_write_lock = threading.Lock()


@dataclass
class AgentDecision:
    """一次 Agent 决策的快照。"""
    timestamp: str                      # ISO8601
    user_id: str = ""
    session_id: str = ""
    source: str = ""                    # react_agent | tool_call | planner
    user_query: str = ""                # 工具层拿不到时留空
    reasoning: str = ""                 # LLM 思考/规划理由（截 500）
    tool_selected: str = ""
    tool_args: dict = field(default_factory=dict)
    execution_time_ms: float = 0.0
    result_summary: str = ""            # 截 200

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _safe(name: str) -> str:
    """user_id 转文件名安全字符。"""
    return re.sub(r"[^A-Za-z0-9_\-.]", "_", name)[:40] or "default"


def log_decision(decision: AgentDecision) -> None:
    """追加一条决策到当日日志文件（线程安全；失败静默）。"""
    with contextlib.suppress(Exception):
        log_dir = Path(get_abs_path("logs")) / "decisions"
        log_dir.mkdir(parents=True, exist_ok=True)
        # 文件名用本地日期（time.strftime 取本地时区），内容时间戳用 UTC ISO8601
        log_file = log_dir / f"{time.strftime('%Y-%m-%d')}_{_safe(decision.user_id or get_user_id())}.jsonl"
        with _write_lock, open(log_file, "a", encoding="utf-8") as f:
            f.write(decision.to_json() + "\n")


def make_decision(**kwargs) -> AgentDecision:
    """便捷构造：自动填 timestamp/user_id/session_id，并截断长文本字段。"""
    kwargs.setdefault("timestamp", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    kwargs.setdefault("user_id", get_user_id())
    kwargs.setdefault("session_id", get_session_id())
    if "reasoning" in kwargs:
        kwargs["reasoning"] = str(kwargs["reasoning"])[:500]
    if "result_summary" in kwargs:
        kwargs["result_summary"] = str(kwargs["result_summary"])[:200]
    return AgentDecision(**kwargs)


_publisher: Callable[[dict], None] | None = None


def set_decision_publisher(publisher: Callable[[dict], None]) -> None:
    """注入 [DECISION] 事件发布器（组合根接线；传 None 撤销）。"""
    global _publisher
    _publisher = publisher


def emit_decision(payload: dict) -> None:
    """把决策经注入的发布器外发为 [DECISION] 事件（未接线/失败均静默）。"""
    publisher = _publisher
    if publisher is None:
        return
    with contextlib.suppress(Exception):
        publisher(payload)
