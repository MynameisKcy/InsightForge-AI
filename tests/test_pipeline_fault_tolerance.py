"""流水线阶段级容错测试（pipeline-fault-tolerance spec）。

覆盖：阶段异常/超时 → 降级占位写入 + completed_steps 排除 + 后续非依赖步骤
仍执行；依赖失败步骤者被跳过；超时 <=0 不阻塞；ReportAgent 识别 error 键
渲染显式"⚠️ 本阶段不可用"而非静默 N/A 缺数据。
用 __new__ 构造 PlannerAgent / ReportAgent 注入桩 handler，避免真实 LLM/agent。
"""
import time
import unittest
from unittest.mock import patch

from agents.pipeline_context import PipelineContext
from agents.planner_agent import PlannerAgent, RequestContext
from agents.report_agent import REPORT_TEMPLATE_PATH, ReportAgent, _basic_markdown_report


def _planner_with_map(agent_map: dict) -> PlannerAgent:
    """构造未初始化的 PlannerAgent，注入桩 _agent_map。"""
    p = PlannerAgent.__new__(PlannerAgent)
    p._agent_map = agent_map
    return p


class ExecuteStepTests(unittest.TestCase):
    def _ctx(self):
        return RequestContext(user_id="test_ft")

    def test_exception_writes_degradation_and_excludes_completed(self):
        def boom(task, pctx, ctx):
            raise RuntimeError("boom")
        p = _planner_with_map({"trend_analysis": boom, "report": lambda t, c, x: None})
        pctx = PipelineContext()
        step = {"step": 1, "agent": "trend_analysis", "task": "t", "depends_on": []}

        status = p._execute_step(step, pctx, self._ctx())

        self.assertEqual(status, "failed")
        self.assertNotIn(1, pctx.completed_steps)
        self.assertTrue(pctx.trend_result)
        self.assertIn("boom", pctx.trend_result["error"])
        self.assertTrue(any("trend_analysis" in e for e in pctx.errors))

    def test_timeout_treated_as_failure_with_degradation(self):
        def slow(task, pctx, ctx):
            time.sleep(0.8)  # 只需明显长于 0.5s 超时；超时后仍残留运行，但不写 pctx → 无竞态
        p = _planner_with_map({"trend_analysis": slow})
        pctx = PipelineContext()
        step = {"step": 1, "agent": "trend_analysis", "task": "t", "depends_on": []}

        with patch("agents.planner_agent._stage_timeout_seconds", return_value=0.5):
            start = time.monotonic()
            status = p._execute_step(step, pctx, self._ctx())
            elapsed = time.monotonic() - start

        self.assertEqual(status, "failed")
        self.assertLess(elapsed, 1.5)  # 0.5s 超时即返回，不等满 slow 的 0.8s
        self.assertNotIn(1, pctx.completed_steps)
        self.assertIn("超时", pctx.trend_result["error"])

    def test_timeout_disabled_when_zero(self):
        done = {"flag": False}
        def slow(task, pctx, ctx):
            time.sleep(0.3)
            done["flag"] = True
        p = _planner_with_map({"trend_analysis": slow})
        pctx = PipelineContext()
        step = {"step": 1, "agent": "trend_analysis", "task": "t", "depends_on": []}

        with patch("agents.planner_agent._stage_timeout_seconds", return_value=0):
            status = p._execute_step(step, pctx, self._ctx())

        self.assertEqual(status, "ok")
        self.assertTrue(done["flag"])
        self.assertIn(1, pctx.completed_steps)

    def test_unknown_agent_writes_degradation(self):
        p = _planner_with_map({})  # 无 trend_analysis 映射
        pctx = PipelineContext()
        step = {"step": 1, "agent": "trend_analysis", "task": "t", "depends_on": []}

        status = p._execute_step(step, pctx, self._ctx())

        self.assertEqual(status, "failed")
        self.assertIn("本阶段不可用", pctx.trend_result["error"])

    def test_ok_marks_completed(self):
        def ok(task, pctx, ctx):
            pctx.trend_result = {"insight": "ok"}
        p = _planner_with_map({"trend_analysis": ok})
        pctx = PipelineContext()
        step = {"step": 1, "agent": "trend_analysis", "task": "t", "depends_on": []}

        status = p._execute_step(step, pctx, self._ctx())

        self.assertEqual(status, "ok")
        self.assertIn(1, pctx.completed_steps)

    def test_deps_ready_skips_unmet(self):
        p = _planner_with_map({})
        pctx = PipelineContext()
        # step 2 依赖 step 1，但 1 未完成
        step2 = {"step": 2, "agent": "report", "task": "t", "depends_on": [1]}
        self.assertFalse(p._deps_ready(step2, pctx))
        pctx.completed_steps.add(1)
        self.assertTrue(p._deps_ready(step2, pctx))


class ReportDegradationTests(unittest.TestCase):
    def _data_with_trend_error(self):
        r = ReportAgent.__new__(ReportAgent)
        r._template = None  # 走 _basic_markdown_report 后备
        return r._build_report_data(
            title="t", task="t", executive_summary="", conclusion="",
            trend_result={"error": "本阶段不可用（boom）"},
            product_result={}, risk_result={}, charts=[],
        )

    def test_basic_markdown_renders_degradation_not_silent_na(self):
        data = self._data_with_trend_error()
        md = _basic_markdown_report(data)
        self.assertIn("⚠️", md)
        self.assertIn("本阶段不可用（boom）", md)
        # 失败阶段不渲染静默 N/A 数据行
        self.assertNotIn("整体趋势方向", md)

    def test_jinja_template_renders_degradation(self):
        from jinja2 import Template
        with open(REPORT_TEMPLATE_PATH, encoding="utf-8") as f:
            tpl = Template(f.read())
        data = self._data_with_trend_error()
        md = tpl.render(**data)
        self.assertIn("⚠️", md)
        self.assertIn("本阶段不可用（boom）", md)
        self.assertNotIn("整体趋势方向", md)

    def test_no_error_renders_normal_section(self):
        r = ReportAgent.__new__(ReportAgent)
        r._template = None
        data = r._build_report_data(
            title="t", task="t", executive_summary="", conclusion="",
            trend_result={"insight": "上升", "direction": "up"},
            product_result={}, risk_result={}, charts=[],
        )
        md = _basic_markdown_report(data)
        self.assertNotIn("⚠️", md)
        self.assertIn("整体趋势方向", md)


if __name__ == "__main__":
    unittest.main()
