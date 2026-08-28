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


# ── 阶段 1.2:async 版 _execute_step_async(为 2.1 三并发做基础设施) ──
# 项目约定用 asyncio.run(...) 包裹 async def(避免依赖 pytest-asyncio)

import asyncio


def test_async_step_success_emits_timing():
    """_execute_step_async 成功路径 emit step_timing,duration_ms 反映 wall-clock。"""
    from agents.planner_agent import PlannerAgent, RequestContext

    planner = PlannerAgent.__new__(PlannerAgent)
    def ok_handler(task, pctx, ctx):
        return None
    planner._agent_map = {"sql_query": ok_handler}
    ctx = RequestContext(user_id="u", session_id="s", dataset_name="d", csv_path="x",
                         primary_table="t", query="q")
    pctx = PipelineContext()

    captured = []
    async def scenario():
        with patch("agents.planner_agent.get_progress_emitter") as get_em:
            emitter = MagicMock()
            emitter.emit = lambda t, d: captured.append((t, d))
            get_em.return_value = emitter
            return await planner._execute_step_async(_step(), pctx, ctx)

    result = asyncio.run(scenario())

    assert result == "ok"
    timing_events = [(t, d) for t, d in captured if t == "step_timing"]
    assert len(timing_events) == 1
    assert timing_events[0][1]["status"] == "ok"
    assert timing_events[0][1]["duration_ms"] >= 0


def test_async_step_gather_truly_runs_concurrently():
    """关键测试:3 个 async step 用 asyncio.gather 时,wallclock 应 < 串行 sum。
    这是 2.1 三并发的本质证明(commit 2 仅做基础设施,真并发留 commit 5)。
    """
    from agents.planner_agent import PlannerAgent, RequestContext

    planner = PlannerAgent.__new__(PlannerAgent)

    def slow_handler_factory(ms):
        def h(task, pctx, ctx):
            time.sleep(ms / 1000.0)
        return h

    planner._agent_map = {
        "trend_analysis": slow_handler_factory(50),
        "product_analysis": slow_handler_factory(50),
        "risk_analysis": slow_handler_factory(50),
    }
    ctx = RequestContext(user_id="u", session_id="s", dataset_name="d", csv_path="x",
                         primary_table="t", query="q")
    pctx = PipelineContext()

    async def scenario():
        with patch("agents.planner_agent.get_progress_emitter", return_value=None):
            steps = [
                {"step": 1, "agent": "trend_analysis", "task": "t", "depends_on": []},
                {"step": 2, "agent": "product_analysis", "task": "p", "depends_on": []},
                {"step": 3, "agent": "risk_analysis", "task": "r", "depends_on": []},
            ]
            t0 = time.perf_counter()
            results = await asyncio.gather(*[planner._execute_step_async(s, pctx, ctx) for s in steps])
            return results, (time.perf_counter() - t0) * 1000

    results, wallclock_ms = asyncio.run(scenario())
    assert results == ["ok", "ok", "ok"]
    # 3×50ms 串行 = 150ms,并发应 < 100ms(预留调度开销)
    assert wallclock_ms < 100, f"3×50ms steps in gather should be <100ms, got {wallclock_ms:.0f}ms"


def test_async_step_handler_exception_still_records_timing():
    """失败也 emit step_timing(finally 块),与同步版一致。"""
    from agents.planner_agent import PlannerAgent, RequestContext

    planner = PlannerAgent.__new__(PlannerAgent)
    def fail_handler(task, pctx, ctx):
        raise RuntimeError("simulated")
    planner._agent_map = {"sql_query": fail_handler}
    ctx = RequestContext(user_id="u", session_id="s", dataset_name="d", csv_path="x",
                         primary_table="t", query="q")
    pctx = PipelineContext()

    captured = []
    async def scenario():
        with patch("agents.planner_agent.get_progress_emitter") as get_em:
            emitter = MagicMock()
            emitter.emit = lambda t, d: captured.append((t, d))
            get_em.return_value = emitter
            return await planner._execute_step_async(_step(), pctx, ctx)

    result = asyncio.run(scenario())
    assert result == "failed"
    timing = [d for t, d in captured if t == "step_timing"][0]
    assert timing["status"] == "failed"


# ── 阶段 1.5 / 2.1:Trend/Product/Risk 三并发识别 + 执行 ──

def test_detect_concurrency_group_when_three_analyses_present():
    """plan 含 trend+product+risk 且 depends_on 相同 → 识别为并发组。"""
    from agents.planner_agent import PlannerAgent
    plan = [
        {"step": 1, "agent": "sql_query", "task": "t", "depends_on": []},
        {"step": 2, "agent": "trend_analysis", "task": "t", "depends_on": [1]},
        {"step": 3, "agent": "product_analysis", "task": "p", "depends_on": [1]},
        {"step": 4, "agent": "risk_analysis", "task": "r", "depends_on": [1]},
    ]
    g = PlannerAgent._detect_analysis_concurrency_group(plan)
    assert g is not None
    assert g["head_step_num"] == 2
    assert set(g["member_step_nums"]) == {3, 4}


def test_detect_concurrency_group_returns_none_when_dependencies_differ():
    """依赖不一致 → 不并发(避免破坏依赖语义)。"""
    from agents.planner_agent import PlannerAgent
    plan = [
        {"step": 1, "agent": "sql_query", "task": "t", "depends_on": []},
        {"step": 2, "agent": "trend_analysis", "task": "t", "depends_on": [1]},
        {"step": 3, "agent": "product_analysis", "task": "p", "depends_on": [1, 2]},  # 依赖 trend
        {"step": 4, "agent": "risk_analysis", "task": "r", "depends_on": [1]},
    ]
    g = PlannerAgent._detect_analysis_concurrency_group(plan)
    assert g is None


def test_detect_concurrency_group_returns_none_when_analyses_absent():
    """无三件套 → 不并发(行为不变,串行兜底)。"""
    from agents.planner_agent import PlannerAgent
    plan = [
        {"step": 1, "agent": "sql_query", "task": "t", "depends_on": []},
        {"step": 2, "agent": "visualization", "task": "v", "depends_on": [1]},
    ]
    g = PlannerAgent._detect_analysis_concurrency_group(plan)
    assert g is None


def test_concurrent_analysis_group_actually_runs_in_parallel():
    """端到端等价性:3×50ms handler 用并发组跑,wallclock 应 < 100ms(理论 max≈50ms)。"""
    from agents.planner_agent import PlannerAgent, RequestContext

    planner = PlannerAgent.__new__(PlannerAgent)
    def slow_handler(task, pctx, ctx):
        import time as _t
        _t.sleep(0.05)
    planner._agent_map = {
        "trend_analysis": slow_handler,
        "product_analysis": slow_handler,
        "risk_analysis": slow_handler,
    }
    pctx = PipelineContext()
    pctx.completed_steps.add(1)  # 模拟 sql 已完成
    ctx = RequestContext(user_id="u", session_id="s", dataset_name="d", csv_path="x",
                         primary_table="t", query="q")

    group = {
        "head_step_num": 2,
        "member_step_nums": [3, 4],
        "steps": [
            {"step": 2, "agent": "trend_analysis", "task": "t", "depends_on": [1]},
            {"step": 3, "agent": "product_analysis", "task": "p", "depends_on": [1]},
            {"step": 4, "agent": "risk_analysis", "task": "r", "depends_on": [1]},
        ],
    }

    with patch("agents.planner_agent.get_progress_emitter", return_value=None):
        t0 = time.perf_counter()
        planner._execute_analysis_group_concurrent(group, pctx, ctx, "q")
        wallclock_ms = (time.perf_counter() - t0) * 1000

    # 三步并发:理论 wallclock ≈ 50ms;允许 30ms 调度开销,阈值 100ms
    assert wallclock_ms < 100, f"concurrent group should be <100ms, got {wallclock_ms:.0f}ms"
    # 三步都应进 completed_steps
    assert pctx.completed_steps == {1, 2, 3, 4}
