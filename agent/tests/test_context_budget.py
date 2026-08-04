"""context_budget 单测（ADR-0003 Phase 2）：token 估算/抽取/上下文窗口解析/阈值。"""
import os
import sys
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from memory import context_budget as cb


class _FakeMsg:
    """模拟 AIMessage：只暴露 response_metadata。"""

    def __init__(self, response_metadata=None):
        self.response_metadata = response_metadata or {}


class EstimateTokensTests(unittest.TestCase):
    def test_estimate_chars_divided_by_ratio(self):
        # 25 字符 / 2.5 = 10 token
        self.assertEqual(cb.estimate_messages_tokens("", [{"role": "user", "content": "a" * 25}]), 10)

    def test_estimate_includes_summary_and_all_turns(self):
        turns = [{"role": "user", "content": "x" * 10}, {"role": "assistant", "content": "y" * 10}]
        # (5 + 10 + 10) / 2.5 = 10
        self.assertEqual(cb.estimate_messages_tokens("z" * 5, turns), 10)


class ExtractInputTokensTests(unittest.TestCase):
    def test_present(self):
        msg = _FakeMsg({"token_usage": {"input_tokens": 26, "output_tokens": 20, "total_tokens": 46}})
        self.assertEqual(cb.extract_input_tokens(msg), 26)

    def test_missing_returns_none(self):
        self.assertIsNone(cb.extract_input_tokens(_FakeMsg({})))
        self.assertIsNone(cb.extract_input_tokens(_FakeMsg({"token_usage": {}})))
        self.assertIsNone(cb.extract_input_tokens(None))


class ContextWindowTests(unittest.TestCase):
    def test_known_model_resolves_from_config(self):
        # qwen-plus 在 model_context.yml 配为 131072
        self.assertEqual(cb.get_context_window_for_name("qwen-plus"), 131072)

    def test_unknown_model_falls_back_to_default(self):
        # 未列出的模型走 default(32768)
        self.assertEqual(cb.get_context_window_for_name("totally-unknown-model"), 32768)

    def test_thresholds_loaded(self):
        self.assertAlmostEqual(cb.compress_threshold(), 0.9)
        self.assertAlmostEqual(cb.char_fallback_threshold(), 0.8)
        self.assertEqual(cb.min_keep_turns(), 3)


if __name__ == "__main__":
    unittest.main()
