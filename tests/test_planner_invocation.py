"""Tests for sub-agent invocation conditions in PlannerAgent (Q-B).

Covers three fixes:
1. Export formats are derived from the user's original query (ctx.query) instead
   of the hardcoded ["md","html"] -- so Word/PDF (supported by ExportAgent, advertised
   by the system prompt) are actually produced when requested.
2. _default_plan: visualization/report depend on SQL (step 1) rather than the
   fragile "[step-1]" (the previously-appended step), so they aren't skipped just
   because an unrelated middle step failed.
3. _default_plan: duplicate "趋势" keyword removed.

These are pure-logic tests; PlannerAgent is constructed via __new__ to avoid the
heavy __init__ (which builds 7 sub-agents + LLM models).
"""
import unittest

from agents.pipeline_context import PipelineContext
from agents.planner_agent import PlannerAgent, RequestContext


def _bare_planner():
    """A PlannerAgent without running __init__ (no sub-agents / models built)."""
    return PlannerAgent.__new__(PlannerAgent)


class ExportFormatResolutionTests(unittest.TestCase):
    def test_word_keyword_maps_to_docx(self):
        self.assertEqual(PlannerAgent._resolve_export_formats("请导出为Word"), ["docx"])

    def test_pdf_and_html(self):
        self.assertEqual(PlannerAgent._resolve_export_formats("导出PDF和HTML"), ["pdf", "html"])

    def test_markdown_keyword(self):
        self.assertEqual(PlannerAgent._resolve_export_formats("导出markdown"), ["md"])

    def test_all_three_named(self):
        self.assertEqual(
            PlannerAgent._resolve_export_formats("导出Word、PDF、HTML"),
            ["docx", "pdf", "html"],
        )

    def test_no_format_keyword_falls_back_to_default(self):
        self.assertEqual(PlannerAgent._resolve_export_formats("生成分析报告"), ["md", "html"])

    def test_empty_query_falls_back_to_default(self):
        self.assertEqual(PlannerAgent._resolve_export_formats(""), ["md", "html"])

    def test_case_insensitive(self):
        self.assertEqual(PlannerAgent._resolve_export_formats("export to WORD and Pdf"),
                         ["docx", "pdf"])


class RunExportWiringTests(unittest.TestCase):
    """_run_export must pass query-derived formats to the export agent."""

    def setUp(self):
        self.planner = _bare_planner()
        self.recorded = {}

        planner = self.planner
        recorded = self.recorded

        class _FakeExport:
            def run(self, input_data):
                recorded["formats"] = input_data.get("formats")
                recorded["title"] = input_data.get("title")
                return {"files": [{"format": "md", "path": "/tmp/x.md"}], "errors": []}

        planner.export_agent = _FakeExport()

    def test_passes_query_derived_formats(self):
        ctx = RequestContext(user_id="u1", query="导出为Word和PDF")
        pctx = PipelineContext(report_result={"markdown": "# 标题", "title": "报告"})
        self.planner._run_export("导出", pctx, ctx)
        self.assertEqual(self.recorded["formats"], ["docx", "pdf"])

    def test_default_formats_when_query_unspecified(self):
        ctx = RequestContext(user_id="u1", query="生成分析报告")
        pctx = PipelineContext(report_result={"markdown": "# 标题", "title": "报告"})
        self.planner._run_export("导出", pctx, ctx)
        self.assertEqual(self.recorded["formats"], ["md", "html"])

    def test_no_markdown_returns_error_without_calling_export(self):
        ctx = RequestContext(user_id="u1", query="导出Word")
        pctx = PipelineContext(report_result={"markdown": "", "title": "报告"})
        self.planner._run_export("导出", pctx, ctx)
        self.assertNotIn("formats", self.recorded)  # export agent never called
        self.assertEqual(pctx.export_result["error"], "No report content to export")
        self.assertEqual(pctx.export_result["files"], [])


class DefaultPlanTests(unittest.TestCase):
    def setUp(self):
        self.planner = _bare_planner()

    def _agents(self, plan):
        return [s["agent"] for s in plan]

    def _step(self, plan, agent):
        for s in plan:
            if s["agent"] == agent:
                return s
        return None

    def test_sql_is_always_first(self):
        plan = self.planner._default_plan("分析销售趋势")
        self.assertEqual(plan[0]["agent"], "sql_query")
        self.assertEqual(plan[0]["depends_on"], [])

    def test_trend_keyword_adds_trend_depending_on_sql(self):
        plan = self.planner._default_plan("分析销售趋势")
        self.assertIn("trend_analysis", self._agents(plan))
        self.assertEqual(self._step(plan, "trend_analysis")["depends_on"], [1])

    def test_visualization_depends_on_sql_not_previous_step(self):
        # "画个趋势图" matches both trend (step 2) and visualization (step 3).
        # Old code set viz depends_on=[step-1]=[2] (trend); fix makes it [1] (SQL),
        # so viz isn't skipped when trend fails.
        plan = self.planner._default_plan("画个趋势图")
        viz = self._step(plan, "visualization")
        self.assertIsNotNone(viz)
        self.assertEqual(viz["depends_on"], [1])

    def test_report_depends_on_sql(self):
        plan = self.planner._default_plan("生成报告")
        report = self._step(plan, "report")
        self.assertIsNotNone(report)
        self.assertEqual(report["depends_on"], [1])

    def test_no_keywords_falls_back_to_full_plan(self):
        plan = self.planner._default_plan("随便看看")
        # fallback: sql + trend + product + visualization + report
        self.assertEqual(self._agents(plan),
                         ["sql_query", "trend_analysis", "product_analysis",
                          "visualization", "report"])

    def test_step_numbers_are_sequential(self):
        plan = self.planner._default_plan("分析销售趋势并画图生成报告")
        nums = [s["step"] for s in plan]
        self.assertEqual(nums, list(range(1, len(plan) + 1)))


class PlanSanitizerTests(unittest.TestCase):
    """_sanitize_plan：LLM 计划的后校验闸（去重/上限/重编号+依赖重映射）。

    背景：弱模型会吐出 product_analysis×4 / visualization×3 的重复计划
    （live 日志 16:26 实锤），原样执行既浪费时长又让前端步骤清单出现
    多个"分组对比分析"。规则：非可视化 agent 全局唯一（保留首个）、
    visualization 上限 3、总步数上限 10；重编号 step 并把 depends_on
    经旧→新映射重建（被删步骤的依赖回指其保留首例）。
    """

    def setUp(self):
        self.planner = _bare_planner()

    @staticmethod
    def _plan(*specs):
        """specs: (agent, deps) 序列 → 标准计划结构。"""
        return [
            {"step": i, "agent": a, "task": f"{a} 任务", "depends_on": list(deps)}
            for i, (a, deps) in enumerate(specs, start=1)
        ]

    def test_log_garbage_plan_collapses(self):
        # live 16:26 的真实垃圾计划：pa×4 + viz×3（viz 未超上限 3，全部保留）
        plan = self._plan(
            ("sql_query", []),
            ("product_analysis", [1]),
            ("product_analysis", [1]),
            ("product_analysis", [1]),
            ("product_analysis", [1]),
            ("visualization", [2]),
            ("visualization", [3]),
            ("visualization", [4]),
            ("report", [2, 3, 4, 5, 6, 7, 8]),
        )
        out = PlannerAgent._sanitize_plan(plan)
        self.assertEqual(
            [(s["agent"], s["depends_on"]) for s in out],
            [
                ("sql_query", []),
                ("product_analysis", [1]),      # pa 去重保首个
                ("visualization", [2]),          # 被删的旧3/旧4 → 回指新2
                ("visualization", [2]),
                ("visualization", [2]),
                ("report", [2, 3, 4, 5]),        # 旧2..8 → 新 {2,3,4,5}
            ],
        )

    def test_dependency_remap_after_dedup(self):
        plan = self._plan(
            ("sql_query", []),
            ("product_analysis", [1]),
            ("product_analysis", [1]),      # 删，依赖回指新 2
            ("visualization", [2]),
            ("visualization", [3]),          # viz 保留，依赖旧3(删)→重映射到新2
            ("report", [4, 5]),              # 重映射为新 [3, 4]
        )
        out = PlannerAgent._sanitize_plan(plan)
        self.assertEqual([s["agent"] for s in out],
                         ["sql_query", "product_analysis", "visualization", "visualization", "report"])
        by_agent = {s["agent"]: s for s in out}
        self.assertEqual(by_agent["product_analysis"]["depends_on"], [1])
        vizzes = [s for s in out if s["agent"] == "visualization"]
        self.assertTrue(all(s["depends_on"] == [2] for s in vizzes))
        self.assertEqual([s for s in out if s["agent"] == "report"][0]["depends_on"], [3, 4])

    def test_non_consecutive_duplicate_removed_keep_first(self):
        plan = self._plan(
            ("sql_query", []),
            ("trend_analysis", [1]),
            ("visualization", [2]),
            ("trend_analysis", [1]),   # 非连续重复，删
            ("report", [3, 4]),
        )
        out = PlannerAgent._sanitize_plan(plan)
        self.assertEqual(
            [s["agent"] for s in out],
            ["sql_query", "trend_analysis", "visualization", "report"],
        )
        report = out[-1]
        # 旧4(被删的第二条 trend) 的依赖回指其保留首例 → 新2；旧3(viz) → 新3
        self.assertEqual(report["depends_on"], [2, 3])

    def test_visualization_cap_three(self):
        plan = self._plan(
            ("sql_query", []),
            ("visualization", [1]),
            ("visualization", [1]),
            ("visualization", [1]),
            ("visualization", [1]),   # 第4张图超限，删
            ("report", [2, 3, 4, 5]),
        )
        out = PlannerAgent._sanitize_plan(plan)
        self.assertEqual(sum(1 for s in out if s["agent"] == "visualization"), 3)

    def test_total_step_cap_ten(self):
        # 14 步唯一名计划（不受去重影响）→ 截断到前 10 步，重编号连续
        unique_agents = ["sql_query"] + [f"custom_stage_{i}" for i in range(1, 13)] + ["report"]
        plan = [{"step": i, "agent": a, "task": "", "depends_on": [i - 1] if i > 1 else []}
                for i, a in enumerate(unique_agents, start=1)]
        out = PlannerAgent._sanitize_plan(plan)
        self.assertEqual(len(out), 10)
        self.assertEqual([s["step"] for s in out], list(range(1, 11)))
        for prev, cur in zip(out, out[1:]):
            self.assertIn(prev["step"], cur["depends_on"])

    def test_healthy_plan_passes_through_renumbered_identity(self):
        healthy = self._plan(
            ("sql_query", []),
            ("product_analysis", [1]),
            ("visualization", [2]),
            ("visualization", [2]),
            ("report", [2, 3, 4]),
        )
        out = PlannerAgent._sanitize_plan(healthy)
        self.assertEqual(out, healthy)


if __name__ == "__main__":
    unittest.main()
