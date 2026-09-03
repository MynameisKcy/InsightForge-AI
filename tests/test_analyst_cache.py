"""Tests for per-user PlannerAgent caching in agent_tools (ADR-0001 fix).

The live analysis path is the `run_full_analysis` tool. It previously cached a
single PlannerAgent() built with user_id=None, so every user's analysis ran on
the default model config. The fix caches a PlannerAgent(user_id) per user and
feeds the tool the current request's user_id. These tests verify the
caching / isolation / invalidation contract and that the tool wires user_id
end-to-end, without constructing the real (heavy) PlannerAgent.
"""
import unittest
from unittest.mock import patch

from agent.tools import agent_tools

# Import request_context via the SAME module name that run_full_analysis
# uses internally (`from utils.request_context import get_user_id`), so the
# ContextVar the tests set is the one the tool actually reads.
from utils.request_context import reset_request_context, set_request_context


class _FakePlannerAgent:
    """Records the user_id it was built with; returns a canned run() result."""

    instances = []  # track every construction (class-level)

    def __init__(self, user_id=None):
        self.user_id = user_id
        # stands in for get_chat_model(user_id) -- the whole point of the fix
        self.model = ("model-for", user_id)
        type(self).instances.append(self)

    def run(self, input_data):
        return {
            "success": True,
            "report": {"markdown": f"report for {self.user_id}"},
            "built_with_user_id": self.user_id,
        }


class _StubAnalyst:
    """固定返回 run() 结果的最小桩（绕开 _get_or_create_analyst 缓存）。"""

    def __init__(self, result):
        self.result = result

    def run(self, input_data):
        return self.result


class RunFullAnalysisFailureTests(unittest.TestCase):
    """run_full_analysis 对「失败但报告已生成」的处理：优先回报告，不吞产出。"""

    def _invoke(self, result):
        from agent.tools import agent_tools
        with patch.object(agent_tools, "_get_or_create_analyst",
                          return_value=_StubAnalyst(result)):
            return agent_tools.run_full_analysis.invoke({"query": "分析"})

    def test_failure_with_report_returns_markdown(self):
        out = self._invoke({
            "success": False,
            "errors": ["SQL 查询失败: near DROP"],
            "report": {"markdown": "# 报告（SQL 阶段不可用）"},
        })
        self.assertIn("# 报告", out)
        self.assertNotIn("分析过程出现错误", out)

    def test_failure_without_report_returns_error_text(self):
        out = self._invoke({
            "success": False,
            "errors": ["SQL 查询失败: near DROP"],
            "report": {},
        })
        self.assertIn("分析过程出现错误", out)
        self.assertIn("SQL 查询失败", out)


class AnalystCacheTests(unittest.TestCase):
    def setUp(self):
        agent_tools.invalidate_analyst()  # clear any cached instances
        _FakePlannerAgent.instances = []
        patcher = patch.object(agent_tools, "PlannerAgent", _FakePlannerAgent)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_distinct_users_get_distinct_instances(self):
        a1 = agent_tools._get_or_create_analyst("u1")
        a2 = agent_tools._get_or_create_analyst("u2")
        self.assertIsNot(a1, a2)
        self.assertEqual(a1.user_id, "u1")
        self.assertEqual(a2.user_id, "u2")

    def test_same_user_returns_cached_instance(self):
        a1 = agent_tools._get_or_create_analyst("u1")
        a2 = agent_tools._get_or_create_analyst("u1")
        self.assertIs(a1, a2)
        self.assertEqual(len(_FakePlannerAgent.instances), 1)

    def test_construction_passes_user_id_not_default(self):
        # ADR-0001 core fix: analyst must be built with the real user_id so its
        # model resolves via get_chat_model(user_id), not the default config.
        analyst = agent_tools._get_or_create_analyst("u1")
        self.assertEqual(analyst.user_id, "u1")
        self.assertEqual(analyst.model, ("model-for", "u1"))
        self.assertEqual(len(_FakePlannerAgent.instances), 1)

    def test_default_user_when_no_user_id(self):
        analyst = agent_tools._get_or_create_analyst(None)
        self.assertEqual(analyst.user_id, "default")

    def test_invalidate_drops_only_target_user(self):
        a1 = agent_tools._get_or_create_analyst("u1")
        a2 = agent_tools._get_or_create_analyst("u2")
        agent_tools.invalidate_analyst("u1")
        a1_new = agent_tools._get_or_create_analyst("u1")
        self.assertIsNot(a1_new, a1)  # u1 rebuilt
        self.assertIs(agent_tools._get_or_create_analyst("u2"), a2)  # u2 untouched

    def test_invalidate_all_clears_every_user(self):
        agent_tools._get_or_create_analyst("u1")
        agent_tools._get_or_create_analyst("u2")
        agent_tools.invalidate_analyst()
        self.assertEqual(agent_tools._analyst_cache, {})


class RunFullAnalysisUserIsolationTests(unittest.TestCase):
    """The live tool must build the analyst with the current request's user_id."""

    def setUp(self):
        agent_tools.invalidate_analyst()
        _FakePlannerAgent.instances = []
        patcher = patch.object(agent_tools, "PlannerAgent", _FakePlannerAgent)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_full_analysis_uses_request_user_id(self):
        token = set_request_context(user_id="u_42", session_id="s1")
        try:
            out = agent_tools.run_full_analysis.invoke({"query": "分析销售趋势"})
        finally:
            reset_request_context(token)
        self.assertTrue(_FakePlannerAgent.instances)
        self.assertEqual(_FakePlannerAgent.instances[-1].user_id, "u_42")
        self.assertIn("report for u_42", out)


class RunFullAnalysisFailureDedupeTests(unittest.TestCase):
    """P2-1 失败去重：同会话同 query 失败后二次调用短路，不再空耗执行。

    8-28 报告 §6.2 现象：run_full_analysis 失败后 ReactAgent 反复重试，
    单次 ~80s 拉长整个流程。硬护栏在工具层：同 (user, session, query)
    已失败则直接返回提示，不重复执行。
    """

    user_id, session_id = "u_dedupe", "s_dedupe"
    query = "分析销售额趋势"

    def tearDown(self):
        agent_tools.clear_analysis_failures()

    def _fail_result(self):
        return _StubAnalyst({"success": False,
                             "errors": ["SQL 查询失败: near DROP"], "report": {}})

    def test_second_same_query_short_circuits(self):
        calls = []

        def factory(uid):
            calls.append(uid)
            return self._fail_result()

        with patch.object(agent_tools, "_get_or_create_analyst", side_effect=factory):
            token = set_request_context(user_id=self.user_id, session_id=self.session_id)
            try:
                first = agent_tools.run_full_analysis.invoke({"query": self.query})
                second = agent_tools.run_full_analysis.invoke({"query": self.query})
            finally:
                reset_request_context(token)
        self.assertIn("分析过程出现错误", first)
        self.assertIn("不再重复执行", second)
        self.assertEqual(len(calls), 1, "第二次同 query 应短路，不再触发 analyst 执行")

    def test_session_clear_allows_retry(self):
        """会话结束清理(react_agent finally)后，同 query 可正常重新分析。"""
        results = iter([
            {"success": False, "errors": ["SQL 查询失败"], "report": {}},
            {"success": True, "report": {"markdown": "# 报告 OK"}},
        ])

        def factory(uid):
            return _StubAnalyst(next(results))

        token = set_request_context(user_id=self.user_id, session_id=self.session_id)
        try:
            with patch.object(agent_tools, "_get_or_create_analyst", side_effect=factory):
                first = agent_tools.run_full_analysis.invoke({"query": self.query})
            self.assertIn("分析过程出现错误", first)
            agent_tools.clear_analysis_failures(self.session_id)  # 等价 react_agent 会话结束
            with patch.object(agent_tools, "_get_or_create_analyst", side_effect=factory):
                second = agent_tools.run_full_analysis.invoke({"query": self.query})
        finally:
            reset_request_context(token)
        self.assertIn("# 报告 OK", second)


if __name__ == "__main__":
    unittest.main()
