"""#5 结构校验层：agents/schemas.py 校验器 + BaseAgent._call_llm_with_schema。

离线测试：LLM 100% mock（patch _call_llm），不触达任何真实模型/网络。
"""
import unittest
from unittest.mock import patch

from agents.base import BaseAgent
from agents.schemas import KNOWN_AGENTS, PLAN_SCHEMA, validate


class ValidatorTests(unittest.TestCase):
    """validate() 全分支：类型/必填/枚举/路径错误/宽松性。"""

    def test_valid_plan_passes(self):
        data = {
            "plan": [
                {"step": 1, "agent": "sql_query", "task": "查数据", "depends_on": []},
                {"step": 2, "agent": "trend_analysis", "task": "看趋势", "depends_on": [1]},
            ],
            "title": "报告",
            "reasoning": "因为要趋势",
        }
        self.assertEqual(validate(data, PLAN_SCHEMA), [])

    def test_missing_plan_reported_at_path(self):
        errs = validate({"title": "x"}, PLAN_SCHEMA)
        self.assertEqual(errs, ["$.plan: 缺少必填字段"])

    def test_missing_agent_in_step(self):
        data = {"plan": [{"step": 1, "task": "查数据"}]}
        errs = validate(data, PLAN_SCHEMA)
        self.assertEqual(errs, ["$.plan[0].agent: 缺少必填字段"])

    def test_unknown_agent_rejected(self):
        data = {"plan": [{"step": 1, "agent": "magic_analysis"}]}
        errs = validate(data, PLAN_SCHEMA)
        self.assertIn("$.plan[0].agent: 值 'magic_analysis' 不在允许范围", errs[0])
        self.assertTrue(all(a in KNOWN_AGENTS for a in KNOWN_AGENTS))

    def test_depends_on_with_strings_rejected(self):
        data = {"plan": [{"step": 1, "agent": "sql_query", "depends_on": ["1", "2"]}]}
        errs = validate(data, PLAN_SCHEMA)
        self.assertIn("应为整数列表", errs[0])
        self.assertIn("$.plan[0].depends_on", errs[0])

    def test_bool_not_acceptable_as_int(self):
        # Python 的 bool 是 int 子类，True 不能伪装成 step=1
        data = {"plan": [{"step": True, "agent": "sql_query"}]}
        errs = validate(data, PLAN_SCHEMA)
        self.assertIn("应为整数", errs[0])

    def test_title_wrong_type(self):
        data = {"plan": [], "title": 42}
        errs = validate(data, PLAN_SCHEMA)
        self.assertEqual(errs, ["$.title: 应为 string，实际为 int"])

    def test_extra_keys_tolerated(self):
        # LLM 常附加说明性字段，强删会逼出更差输出
        data = {"plan": [], "notes": "随便写", "agent_thought": {"a": 1}}
        self.assertEqual(validate(data, PLAN_SCHEMA), [])

    def test_object_given_list(self):
        errs = validate([1, 2], {"type": "object", "required": []})
        self.assertEqual(errs, ["$: 应为 object，实际为 list"])

    def test_empty_schema_is_permissive(self):
        self.assertEqual(validate({"raw": "garbage"}, {}), [])

    def test_float_accepts_int_rejects_bool(self):
        self.assertEqual(validate(3, {"type": "float"}), [])
        self.assertEqual(validate(True, {"type": "float"}), ["$: 应为数值，实际为 True"])

    def test_int_list_rejects_str_elements(self):
        self.assertEqual(validate([1, 2], {"type": "int_list"}), [])
        errs = validate([1, "2"], {"type": "int_list"})
        self.assertIn("非整数元素", errs[0])


class SchemaRetryTests(unittest.TestCase):
    """BaseAgent._call_llm_with_schema：携错重试 1 次，重试耗尽返回 None。"""

    def _agent(self):
        return BaseAgent(model=object())

    def test_first_bad_then_good_returns_parsed(self):
        ag = self._agent()
        calls = []

        def fake_llm(messages):
            calls.append(messages)
            if len(calls) == 1:
                return '{"plan": [{"agent": "magic"}]}'   # agent 越界
            return '{"plan": [{"agent": "sql_query", "step": 1, "depends_on": []}]}'

        with patch.object(ag, "_call_llm", side_effect=fake_llm):
            out = ag._call_llm_with_schema([{"role": "user", "content": "plan"}], PLAN_SCHEMA)
        self.assertEqual(out["plan"][0]["agent"], "sql_query")
        self.assertEqual(len(calls), 2)
        # 重试提示词携带具体校验错误（而非裸「请严格按 JSON」）
        self.assertIn("未通过结构校验", calls[1][-1]["content"])
        self.assertIn("magic", calls[1][-1]["content"])

    def test_both_bad_returns_none(self):
        ag = self._agent()
        with patch.object(ag, "_call_llm", return_value='{"plan": [{"agent": "nope"}]}'):
            out = ag._call_llm_with_schema([{"role": "user", "content": "plan"}], PLAN_SCHEMA)
        self.assertIsNone(out)

    def test_retries_zero_means_no_second_call(self):
        ag = self._agent()
        with patch.object(ag, "_call_llm", return_value='{"plan": []}') as m:
            out = ag._call_llm_with_schema(
                [{"role": "user", "content": "plan"}],
                {"type": "object", "required": ["plan", "title"]},
                retries=0,
            )
        self.assertIsNone(out)
        m.assert_called_once()

    def test_llm_exception_propagates(self):
        ag = self._agent()
        with patch.object(ag, "_call_llm", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ag._call_llm_with_schema([{"role": "user", "content": "x"}], PLAN_SCHEMA)


if __name__ == "__main__":
    unittest.main()
