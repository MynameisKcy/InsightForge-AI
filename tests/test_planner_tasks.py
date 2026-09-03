"""Planner Task System（#1）：run() 持久化接线 + resume() 续跑语义。

离线测试：bare planner（__new__ 跳过 __init__）+ fake 子 Agent + tmp tasks root。
```
"""
import tempfile
import unittest
from unittest.mock import patch

from agents.planner_agent import PlannerAgent, RequestContext
from memory.task_store import TaskRecord, get_task, new_task_id, save_task, set_tasks_root


def _bare_planner():
    return PlannerAgent.__new__(PlannerAgent)


def _fake_agents(planner, recorder: dict):
    """挂 7 个 fake 子 Agent（record run 调用），并重建 _agent_map。"""
    class _Fake:
        def __init__(self, name):
            self.name = name

        def run(self, input_data):
            recorder.setdefault(self.name, []).append(input_data)
            return {"error": None, "dataframe_json": "[{}]", "name": self.name}

    planner.sql_agent = _Fake("sql_query")
    planner.trend_agent = _Fake("trend_analysis")
    planner.product_agent = _Fake("product_analysis")
    planner.risk_agent = _Fake("risk_analysis")
    planner.viz_agent = _Fake("visualization")
    planner.report_agent = _Fake("report")
    planner.export_agent = _Fake("export")
    planner._agent_map = {
        "sql_query": planner._run_sql,
        "trend_analysis": planner._run_trend,
        "product_analysis": planner._run_product,
        "risk_analysis": planner._run_risk,
        "visualization": planner._run_visualization,
        "report": planner._run_report,
        "export": planner._run_export,
    }


_PLAN = [
    {"step": 1, "agent": "sql_query", "task": "查数据", "depends_on": []},
    {"step": 2, "agent": "trend_analysis", "task": "看趋势", "depends_on": [1]},
    {"step": 3, "agent": "report", "task": "出报告", "depends_on": [2]},
]


class PlannerRunPersistenceTests(unittest.TestCase):
    """run() 应在计划定稿后建记录、逐步持久化、终态落盘。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="planner_task_test_")
        set_tasks_root(self.tmp)
        self.addCleanup(lambda: set_tasks_root(None))
        # 离线约束：run() 末尾 Goal 判断器默认开，会构造真实 LLM——
        # 测试统一关闭（create=True：P1 期该函数尚不存在时退化为无害 no-op，
        # P2 期函数存在则真实屏蔽；使本文件可在两期分别提交且各自通过）
        self._ev = patch("agents.planner_agent._goal_evaluator_enabled",
                         return_value=False, create=True)
        self._ev.start()
        self.addCleanup(self._ev.stop)
        self.recorder = {}
        self.planner = _bare_planner()
        _fake_agents(self.planner, self.recorder)
        self.planner._create_plan = lambda q, h: {
            "plan": _PLAN, "title": "报告", "reasoning": "r"}
        self.planner._rewrite_query = lambda q, uid, sid: q
        self.planner._resolve_context = lambda inp: RequestContext(
            user_id=inp.get("user_id", "u1"), session_id="s1",
            query=inp.get("query", ""), primary_table="DS")

    def test_run_creates_and_finalizes_task(self):
        result = self.planner.run({"query": "分析趋势", "user_id": "u1"})
        task_id = result.get("task_id")
        self.assertIsNotNone(task_id)
        rec = get_task("u1", task_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.status, "completed")
        self.assertEqual(rec.completed_steps, [1, 2, 3])
        # sql 步结果快照进 stage_results 且 dataframe_json 已存
        self.assertIn("sql_query", rec.stage_results)
        self.assertEqual(rec.dataframe_json, "[{}]")
        # ⑤ 去冗余：stage_results 的 sql_query 条目不重复存 dataframe_json 大字段
        self.assertNotIn("dataframe_json", rec.stage_results["sql_query"])

    def test_sql_failure_marks_failed_but_report_generated(self):
        """SQL agent 返回错误（非异常）：success=False + 任务 failed，
        但报告仍生成（ReportAgent 渲染本阶段不可用），工具路径可继续回报告。"""
        class _BadSQL:
            def run(self, input_data):
                return {"error": "SQL 语法错误: near DROP", "dataframe_json": None}

        self.planner.sql_agent = _BadSQL()
        result = self.planner.run({"query": "分析趋势", "user_id": "u1"})
        self.assertFalse(result["success"])
        self.assertTrue(any("SQL 查询失败" in e for e in result["errors"]))
        # 报告照常产出（report 步骤不受 errors 影响，依赖只看 completed_steps）
        self.assertTrue(result.get("report"))  # fake report_agent 返回了 report_result
        # 任务终态如实标记 failed
        rec = get_task("u1", result["task_id"])
        self.assertEqual(rec.status, "failed")

    def test_run_task_best_effort_does_not_break_pipeline(self):
        # 存储层故障（非法 owner）时流水线照常返回结果
        result = self.planner.run({"query": "x", "user_id": "../bad"})
        self.assertTrue(result["success"])


class PlannerResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="planner_resume_test_")
        set_tasks_root(self.tmp)
        self.addCleanup(lambda: set_tasks_root(None))
        self.recorder = {}
        self.planner = _bare_planner()
        _fake_agents(self.planner, self.recorder)

    def _saved(self, *, completed=None, status="running", primary_table="DS",
               plan=None, dataframe_json="[{\"a\":1}]", session_id=""):
        rec = TaskRecord(
            id=new_task_id(), owner="u1", query="分析趋势", title="报告",
            plan=plan or _PLAN,
            completed_steps=completed or [],
            stage_results={"sql_query": {"error": None, "name": "sql_query"}},
            dataframe_json=dataframe_json,
            dataset_name="DS", primary_table=primary_table,
            status=status, session_id=session_id,
        )
        return save_task(rec)

    def test_resume_skips_completed_runs_remaining(self):
        rec = self._saved(completed=[1])  # sql 已完成，只跑 trend + report
        with patch("database.data_resolver.DataResolver.resolve",
                   return_value={"name": "DS", "csv_path": "", "description": ""}):
            result = self.planner.resume(rec.id, "u1", "s1")
        self.assertTrue(result["success"])
        self.assertEqual(result.get("resumed"), True)
        # sql 不重跑；trend/report 各跑一次
        self.assertNotIn("sql_query", self.recorder)
        self.assertEqual(len(self.recorder.get("trend_analysis", [])), 1)
        self.assertEqual(len(self.recorder.get("report", [])), 1)
        # 终态落盘
        got = get_task("u1", rec.id)
        self.assertEqual(got.status, "completed")
        self.assertEqual(got.completed_steps, [1, 2, 3])

    def test_resume_completed_task_is_idempotent(self):
        rec = self._saved(completed=[1, 2, 3], status="completed")
        with patch("database.data_resolver.DataResolver.resolve",
                   return_value={"name": "DS", "csv_path": "", "description": ""}):
            result = self.planner.resume(rec.id, "u1", "s1")
        self.assertTrue(result["success"])
        self.assertEqual(self.recorder, {})  # 任何 agent 都不重跑

    def test_resume_missing_task_returns_error(self):
        result = self.planner.resume("task_ghost", "u1", "s1")
        self.assertFalse(result["success"])
        self.assertIn("不存在", result["error"])

    def test_resume_cross_owner_returns_error(self):
        rec = self._saved()
        result = self.planner.resume(rec.id, "u2", "s1")  # u2 无权
        self.assertFalse(result["success"])

    def test_resume_dataset_mismatch_rejected(self):
        rec = self._saved()
        with patch("database.data_resolver.DataResolver.resolve",
                   return_value={"name": "NEW_DS", "csv_path": "", "description": ""}):
            result = self.planner.resume(rec.id, "u1", "s1")
        self.assertFalse(result["success"])
        self.assertIn("数据集已变化", result["error"])

    def test_resume_cancelled_task_rejected(self):
        rec = self._saved(status="cancelled")
        with patch("database.data_resolver.DataResolver.resolve",
                   return_value={"name": "DS", "csv_path": "", "description": ""}):
            result = self.planner.resume(rec.id, "u1", "s1")
        self.assertFalse(result["success"])
        self.assertIn("已取消", result["error"])

    def test_resume_session_id_falls_back_to_task_record(self):
        # 前端不带 session_id（body={}）时回退任务记录原会话
        rec = self._saved(completed=[1], session_id="sess_orig")
        with patch("database.data_resolver.DataResolver.resolve",
                   return_value={"name": "DS", "csv_path": "", "description": ""}):
            result = self.planner.resume(rec.id, "u1", "")
        self.assertTrue(result["success"])
        self.assertEqual(result["session_id"], "sess_orig")

    def test_resume_session_id_explicit_overrides(self):
        rec = self._saved(completed=[1], session_id="sess_orig")
        with patch("database.data_resolver.DataResolver.resolve",
                   return_value={"name": "DS", "csv_path": "", "description": ""}):
            result = self.planner.resume(rec.id, "u1", "sess_new")
        self.assertTrue(result["success"])
        self.assertEqual(result["session_id"], "sess_new")


if __name__ == "__main__":
    unittest.main()
