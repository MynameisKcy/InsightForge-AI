"""parse_json_list 单元测试：LLM 输出 JSON 数组的各种形态（裸/围栏/带杂讯）。

背景：qwen3.7-max 等模型会把 JSON 数组包在 ```json 围栏里输出，
直接 json.loads 失败导致图表决策静默变空（2026-08-21 实测发现）。
"""
import os
import sys
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from agent.agents.base import parse_json_list
except ModuleNotFoundError:
    from agents.base import parse_json_list


class ParseJsonListTests(unittest.TestCase):
    def test_bare_array(self):
        self.assertEqual(parse_json_list('[{"a": 1}]'), [{"a": 1}])

    def test_fenced_json_array(self):
        text = '```json\n[{"chart_type": "line", "title": "趋势"}]\n```'
        self.assertEqual(parse_json_list(text), [{"chart_type": "line", "title": "趋势"}])

    def test_fenced_array_without_lang_tag(self):
        text = "```\n[1, 2, 3]\n```"
        self.assertEqual(parse_json_list(text), [1, 2, 3])

    def test_array_with_surrounding_prose(self):
        text = '好的，以下是图表规格：\n[{"x": 1}]\n希望对你有帮助。'
        self.assertEqual(parse_json_list(text), [{"x": 1}])

    def test_object_returns_empty(self):
        # 对象不是数组：返回 []（对象解析归 _parse_json 管）
        self.assertEqual(parse_json_list('{"a": 1}'), [])
        self.assertEqual(parse_json_list('```json\n{"a": 1}\n```'), [])

    def test_garbage_returns_empty(self):
        self.assertEqual(parse_json_list("这不是 JSON"), [])
        self.assertEqual(parse_json_list(""), [])
        self.assertEqual(parse_json_list(None), [])


if __name__ == "__main__":
    unittest.main()
