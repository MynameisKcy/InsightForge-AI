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

# IMPORTANT: import request_context via the SAME module name that run_full_analysis
# uses internally (`from utils.request_context import get_user_id`). This repo's
# dual-path imports can load agent/utils/request_context.py under two module names
# ("utils.request_context" vs "agent.utils.request_context"); each gets its own
# ContextVar, so setting on one would be invisible to code reading the other.
from utils.request_context import set_request_context, reset_request_context


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


if __name__ == "__main__":
    unittest.main()
