"""WorkflowRunner：请求级 agent 执行边界（s16 风格，#3）。

三职责，不重复 _execute_step 已有的超时/降级/重试编排：
1. 结构校验：包装 BaseAgent._call_llm_with_schema（schema 校验 + 携错重试）；
2. journal：每次调用记 {label, phase, status, duration_ms, error, at} 到
   PipelineContext.journal——P1 TaskRecord 持久化的数据源；
3. 结果缓存：key = label + prompt 规范化哈希（messages 的确定性函数，
   等价覆盖规格中 dataframe_json+task 组合且对 insight 调用更精确）。
   命中即跳过 LLM。缓存随 PipelineContext 请求作用域生存。

落缝位置：AnalysisAgent 内部（insight 生成），不在 planner——prompt 构造权
属于 AnalysisModule 适配器（ADR-0001 模块分解），planner 不再吸收执行逻辑。
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.base import BaseAgent


def _cache_key(label: str, messages: list[dict]) -> str:
    """label + messages 规范化哈希：同 prompt 同输出期望，是缓存正确性的最小键。"""
    blob = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    return f"{label}:{hashlib.sha1(blob.encode('utf-8')).hexdigest()}"


class WorkflowRunner:
    """单次分析请求的执行边界。journal/cache 由 PipelineContext 持有并传入
    （共享同一份引用）；独立使用（无 pctx）时自建本地实例，请求结束即弃。"""

    def __init__(self, journal: list[dict] | None = None,
                 cache: dict | None = None):
        self.journal = journal if journal is not None else []
        self._cache: dict[str, dict] = cache if cache is not None else {}

    def agent(self, caller: "BaseAgent", messages: list[dict], schema: dict,
              label: str, phase: str = "Analyze") -> dict | None:
        """执行一次带结构校验的 LLM 调用。返回校验通过的 dict；失败 None；
        caller 内部异常照常上抛（journal 留痕后 re-raise，不吞错）。"""
        key = _cache_key(label, messages)
        if key in self._cache:
            self._append(label, phase, "cache_hit", 0.0, None)
            return self._cache[key]

        start = time.perf_counter()
        try:
            data = caller._call_llm_with_schema(messages, schema)
        except Exception as e:  # journal 留痕后原样上抛（调用方 except 降级）
            self._append(label, phase, "error",
                         (time.perf_counter() - start) * 1000, str(e)[:200])
            raise
        duration_ms = (time.perf_counter() - start) * 1000

        if data is None:
            self._append(label, phase, "failed", duration_ms,
                         "schema 校验未通过（含 1 次携错重试）")
            return None
        self._append(label, phase, "ok", duration_ms, None)
        self._cache[key] = data
        return data

    def _append(self, label: str, phase: str, status: str,
                duration_ms: float, error: str | None) -> None:
        self.journal.append({
            "label": label,
            "phase": phase,
            "status": status,
            "duration_ms": round(duration_ms, 1),
            "error": error,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
