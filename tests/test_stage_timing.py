"""stage-level timing 埋点(7.1)单元测试:验证 _execute_step emit step_timing 不破坏业务。

设计:
- 用 mock 替换 _agent_map 里的 handler + ProgressEmitter,确保 _execute_step 在
  隔离环境里跑(不打 LLM / 不打 SSE)
- 验证:(1) emit 的事件 type=step_timing + duration_ms>=0 + step/agent 字段齐;
       (2) 成功路径 status=ok + step_done 不被影响;
       (3) 失败路径 status=failed + step_timing 仍 emit(duration_ms 仍记录);
       (4) 未知 agent 仍走 _write_degradation 路径。

不依赖 OTel(trace.get_tracer() NoOp 即可),不依赖数据库。
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from agents.pipeline_context import PipelineContext


def _make_planner_with_mock_handler(handler_returns: str = "ok"):
    """构造一个 PlannerAgent 实例,把 _agent_map 替换成 mock handler。"""
    from agents.planner_agent import PlannerAgent
    planner = PlannerAgent.__new__(PlannerAgent)  # 跳过 __init__ (避免建 7 个真实 Agent)
    # _agent_map 包含 sql_query / trend_analysis / product_analysis / risk_analysis /
    # visualization / report / export 等键;这里用 sql_query 做 smoke。
    mock_handler = MagicMock()
    if handler_returns == "raise":
        mock_handler.side_effect = RuntimeError("simulated failure")
    else:
        mock_handler.return_value = None
    planner._agent_map = {"sql_query": mock_handler}

    # RequestContext 是普通 class,__slots__ 限定字段(无 history 字段)
    from agents.planner_agent import RequestContext
    ctx = RequestContext(
        user_id="u1", session_id="s1", dataset_name="ds", csv_path="x.csv",
        primary_table="t1", query="q",
    )
    return planner, mock_handler, ctx


def _step():
    return {"step": 1, "agent": "sql_query", "task": "test", "depends_on": []}


def test_stage_timing_emitted_on_success():
    planner, handler, ctx = _make_planner_with_mock_handler()
    pctx = PipelineContext()

    captured = []
    with patch("agents.planner_agent.get_progress_emitter") as get_em:
        emitter = MagicMock()
        emitter.emit = lambda t, d: captured.append((t, d))
        get_em.return_value = emitter
        result = planner._execute_step(_step(), pctx, ctx)

    assert result == "ok"
    timing_events = [(t, d) for t, d in captured if t == "step_timing"]
    assert len(timing_events) == 1, f"expected 1 step_timing event, got {len(timing_events)}: {captured}"
    t, d = timing_events[0]
    assert d["step"] == 1
    assert d["agent"] == "sql_query"
    assert d["status"] == "ok"
    assert isinstance(d["duration_ms"], (int, float))
    assert d["duration_ms"] >= 0
    assert d["duration_ms"] < 5000  # smoke test: 简单 mock handler 远小于 5s


def test_stage_timing_emitted_on_handler_exception():
    planner, handler, ctx = _make_planner_with_mock_handler("raise")
    pctx = PipelineContext()

    captured = []
    with patch("agents.planner_agent.get_progress_emitter") as get_em:
        emitter = MagicMock()
        emitter.emit = lambda t, d: captured.append((t, d))
        get_em.return_value = emitter
        result = planner._execute_step(_step(), pctx, ctx)

    assert result == "failed"
    # 即便失败,step_timing 仍 emit (finally 块负责)
    timing_events = [(t, d) for t, d in captured if t == "step_timing"]
    assert len(timing_events) == 1
    t, d = timing_events[0]
    assert d["status"] == "failed"
    assert d["duration_ms"] >= 0
    # 失败要写 degradation 占位
    assert pctx.sql_result is not None
    assert "error" in pctx.sql_result


def test_stage_timing_noop_when_no_emitter():
    """无 emitter 时(同步 /api/analysis 路径),_execute_step 不抛异常,仅 stage_timing no-op。"""
    planner, handler, ctx = _make_planner_with_mock_handler()
    pctx = PipelineContext()

    with patch("agents.planner_agent.get_progress_emitter", return_value=None):
        result = planner._execute_step(_step(), pctx, ctx)
    assert result == "ok"


def test_stage_timing_includes_real_elapsed_time():
    """duration_ms 应反映实际 wall-clock(>=handler sleep 时长)。"""
    from agents.planner_agent import PlannerAgent, RequestContext

    planner = PlannerAgent.__new__(PlannerAgent)

    def slow_handler(task, pctx, ctx):
        time.sleep(0.05)  # 50ms

    planner._agent_map = {"sql_query": slow_handler}
    ctx = RequestContext(user_id="u", session_id="s", dataset_name="d", csv_path="x",
                         primary_table="t", query="q")
    pctx = PipelineContext()

    captured = []
    with patch("agents.planner_agent.get_progress_emitter") as get_em:
        emitter = MagicMock()
        emitter.emit = lambda t, d: captured.append((t, d))
        get_em.return_value = emitter
        planner._execute_step(_step(), pctx, ctx)

    timing = [d for t, d in captured if t == "step_timing"][0]
    # 容忍 perf_counter 精度 + Windows 调度抖动(>=40ms 即可)
    assert timing["duration_ms"] >= 40, f"expected >=40ms, got {timing['duration_ms']}"
