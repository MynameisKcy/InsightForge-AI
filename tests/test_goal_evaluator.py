"""Goal 独立判断器（#2，P2）：fail-open 语义 + planner run() 接线。

离线测试：mock _call_llm / 换桩 GoalEvaluator；不触达真实 LLM。
"""
import tempfile
import unittest
from unittest.mock import patch

from agents.goal_evaluator import GoalEvaluator
from agents.planner_agent import RequestContext
from memory.task_store import set_tasks_root
from tests.test_planner_tasks import _PLAN, _bare_planner, _fake_agents


class _FakeLLM:
    """可编程 fake：返回固定文本流 / 抛异常 / 记录调用。"""

    def __init__(self, responses=None, exc=None):
        self.responses = list(responses or [])
        self.exc = exc
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        if self.exc is not None:
            raise self.exc
        if self.responses:
            return self.responses.pop(0)
        return '{"goal_met": true}'


class GoalEvaluatorTests(unittest.TestCase):
    def test_valid_output_passthrough(self):
        llm = _FakeLLM(responses=['{"goal_met": false, "gap": "没分析原因", '
                                  '"suggested_followup": "补风险分析"}'])
        ev = GoalEvaluator(model=object())
        with patch.object(ev._agent, "_call_llm", side_effect=llm):
            out = ev.evaluate("找出下降原因", {"title": "t", "report_head": "x"})
        self.assertFalse(out["goal_met"])
        self.assertEqual(out["gap"], "没分析原因")
        self.assertEqual(out["suggested_followup"], "补风险分析")

    def test_schema_failure_fail_open(self):
        # 输出非 JSON / goal_met 缺失 → 重试仍坏 → fail-open goal_met=True
        llm = _FakeLLM(responses=["not json at all", '{"goal_met": "maybe"}'])
        ev = GoalEvaluator(model=object())
        with patch.object(ev._agent, "_call_llm", side_effect=llm):
            out = ev.evaluate("目标", {"title": "t"})
        self.assertTrue(out["goal_met"])
        self.assertIn("note", out)

    def test_llm_exception_fail_open(self):
        ev = GoalEvaluator(model=object())
        with patch.object(ev._agent, "_call_llm",
                         side_effect=_FakeLLM(exc=RuntimeError("boom"))):
            out = ev.evaluate("目标", {"title": "t"})
        self.assertTrue(out["goal_met"])
        self.assertIn("boom", out["note"])

    def test_empty_digest_fail_open(self):
        ev = GoalEvaluator(model=object())
        out = ev.evaluate("目标", {})
        self.assertTrue(out["goal_met"])
        self.assertEqual(out["note"], "no digest")


class PlannerGoalWiringTests(unittest.TestCase):
    """run() 末尾 goal_check 接线：成功/失败/开关三路径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="goal_wire_test_")
        set_tasks_root(self.tmp)
        self.addCleanup(lambda: set_tasks_root(None))
        self.recorder = {}
        self.planner = _bare_planner()
        _fake_agents(self.planner, self.recorder)
        self.planner._create_plan = lambda q, h: {
            "plan": _PLAN, "title": "报告", "reasoning": "r"}
        self.planner._rewrite_query = lambda q, uid, sid: q
        self.planner._resolve_context = lambda inp: RequestContext(
            user_id=inp.get("user_id", "u1"), session_id="s1",
            query=inp.get("query", ""), primary_table="DS")
        # 默认打开评估器（接线路径可被测试）
        self._ev_on = patch("agents.planner_agent._goal_evaluator_enabled",
                            return_value=True)
        self._ev_on.start()
        self.addCleanup(self._ev_on.stop)

    def test_success_attaches_goal_check(self):
        with patch("agents.goal_evaluator.GoalEvaluator") as Klass:
            Klass.return_value.evaluate.return_value = {
                "goal_met": False, "gap": "缺原因分析",
                "suggested_followup": "补充风险分析"}
            result = self.planner.run({"query": "找下降原因", "user_id": "u1"})
        gc = result.get("goal_check")
        self.assertIsNotNone(gc)
        self.assertFalse(gc["goal_met"])
        self.assertEqual(gc["gap"], "缺原因分析")
        # 评估器收到原始 query + digest
        evaluate_calls = Klass.return_value.evaluate.call_args_list
        self.assertEqual(evaluate_calls[0][0][0], "找下降原因")
        self.assertIn("report_head", evaluate_calls[0][0][1])

    def test_failure_path_deterministic_without_llm(self):
        # 使 sql 阶段抛异常 → pctx.errors 非空 → goal_check 直接按 errors 出结论，不碰 LLM
        def boom_run(input_data):
            raise RuntimeError("query failed")

        self.planner.sql_agent.run = boom_run
        with patch("agents.goal_evaluator.GoalEvaluator") as Klass:
            result = self.planner.run({"query": "x", "user_id": "u1"})
        self.assertFalse(result["success"])
        self.assertFalse(result["goal_check"]["goal_met"])
        self.assertIn("阶段错误", result["goal_check"]["gap"])
        Klass.assert_not_called()

    def test_config_off_skips_evaluator(self):
        self._ev_on.stop()
        on = patch("agents.planner_agent._goal_evaluator_enabled", return_value=False)
        on.start()
        self.addCleanup(on.stop)
        with patch("agents.goal_evaluator.GoalEvaluator") as Klass:
            result = self.planner.run({"query": "x", "user_id": "u1"})
        self.assertIsNone(result.get("goal_check"))
        Klass.assert_not_called()

    def test_evaluator_crash_fail_open_keeps_result(self):
        def boom(*a, **k):
            raise RuntimeError("eval crash")

        with patch("agents.goal_evaluator.GoalEvaluator",
                   side_effect=boom):
            result = self.planner.run({"query": "x", "user_id": "u1"})
        self.assertTrue(result["success"])            # 分析结果不被吞
        self.assertEqual(result["goal_check"]["note"], "goal evaluator unavailable")


if __name__ == "__main__":
    unittest.main()
