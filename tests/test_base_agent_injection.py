import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.base import BaseAgent
from agents.analysis_agent import AnalysisAgent
from agents.report_agent import ReportAgent


class ModelInjectionTests(unittest.TestCase):
    def test_injected_model_is_used(self):
        fake = object()
        self.assertIs(BaseAgent(model=fake).model, fake)

    def test_factory_when_no_model(self):
        with patch("agents.base.get_chat_model", return_value="FACTORY") as gm:
            self.assertEqual(BaseAgent().model, "FACTORY")
            gm.assert_called_once()

    def test_analysis_agent_forwards_model(self):
        fake = object()
        self.assertIs(AnalysisAgent(analyzer=None, model=fake).model, fake)

    def test_report_agent_forwards_model(self):
        fake = object()
        self.assertIs(ReportAgent(model=fake).model, fake)


class PlannerInjectionTests(unittest.TestCase):
    def test_subagents_share_planner_model(self):
        """reach-around 已删：子 Agent 经构造注入得到 planner 的模型（同一对象）。"""
        from agents.planner_agent import PlannerAgent
        with patch("agents.base.get_chat_model", return_value="USER_MODEL"):
            planner = PlannerAgent(user_id="u1")
        self.assertEqual(planner.model, "USER_MODEL")
        for sub in (planner.sql_agent, planner.trend_agent, planner.product_agent,
                    planner.risk_agent, planner.viz_agent, planner.report_agent,
                    planner.export_agent):
            self.assertIs(sub.model, planner.model)


if __name__ == "__main__":
    unittest.main()
