"""孤儿图表治理回归测试：阶段超时弃用后，可视化落库前实时自查止损。

超时语义是"放弃线程"——被弃线程的 LLM 决策稍后返回会继续生成图表并
save_chart 落库，用户已收到该阶段报错却仍见图表迟到出现（存量问题）。
修复：planner 超时路径在 pctx.abandoned_agents 打标；visualization agent
在生成前与逐图落库前实时自查，命中即跳过持久化。全离线（stub LLM/渲染）。
"""
import threading
import time

import pandas as pd

import agents.visualization_agent as viz_mod
from agents.pipeline_context import PipelineContext
from agents.planner_agent import PlannerAgent
from agents.visualization_agent import VisualizationAgent


def _make_viz(monkeypatch, save_calls):
    """构建全桩可视化 agent：LLM 决策/渲染/知识库落库均可控。"""
    v = VisualizationAgent(model=object())   # model 注入桩，绕开 factory
    spec = {"title": "t", "chart_type": "bar", "x_col": "a", "y_col": "b", "reason": "r"}
    monkeypatch.setattr(v, "_decide_charts", lambda df, task, extra: [spec])
    monkeypatch.setattr(v, "_generate_chart", lambda df, spec, extra: "charts/fake.svg")
    monkeypatch.setattr(viz_mod, "chart_png_path", lambda path: "charts/fake.png")
    monkeypatch.setattr(viz_mod.chart_knowledge, "save_chart",
                        lambda entry, user_id=None: save_calls.append(entry))
    # start_png_batch 在 run 内局部 import，须桩源头模块
    import visualization.charts as charts_mod
    monkeypatch.setattr(charts_mod, "start_png_batch", lambda: None)
    return v


def _run_viz(v, pctx):
    df = pd.DataFrame([{"a": 1, "b": 2}])
    return v.run({
        "dataframe_json": df.to_json(orient="records"),
        "task": "测试任务",
        "pipeline_context": pctx,
        "user_id": "u_orphan",
    })


def test_abandoned_before_generate_skips_persistence(monkeypatch):
    """超时弃用标记已打 → 生成与落库全部跳过，无孤儿图表。"""
    save_calls = []
    v = _make_viz(monkeypatch, save_calls)
    pctx = PipelineContext()
    pctx.abandoned_agents.add("visualization")   # planner 超时路径打的标
    result = _run_viz(v, pctx)
    assert result["charts"] == []
    assert result["error"] is None
    assert save_calls == []


def test_not_abandoned_persists_normally(monkeypatch):
    """未弃用的正常路径不受影响：图表照常生成并落库（防误杀）。"""
    save_calls = []
    v = _make_viz(monkeypatch, save_calls)
    result = _run_viz(v, PipelineContext())
    assert len(result["charts"]) == 1
    assert result["charts"][0]["path"] == "charts/fake.svg"
    assert len(save_calls) == 1


def test_planner_timeout_marks_abandoned(monkeypatch):
    """planner 阶段超时路径必须在返回 failed 前打弃用标记（同步版）。"""
    import agents.planner_agent as pa

    planner = PlannerAgent()
    started = threading.Event()

    def slow_handler(task, pctx, ctx):
        started.set()
        time.sleep(0.6)   # 模拟被放弃后仍会跑完的 LLM 调用

    monkeypatch.setattr(pa, "_stage_timeout_seconds", lambda: 0.1)
    monkeypatch.setattr(planner, "_agent_map",
                        {**planner._agent_map, "sql_query": slow_handler})
    pctx = PipelineContext()
    status = planner._execute_step(
        {"agent": "sql_query", "task": "t", "step": 1}, pctx, None)
    assert status == "failed"
    assert started.is_set()
    assert "sql_query" in pctx.abandoned_agents
