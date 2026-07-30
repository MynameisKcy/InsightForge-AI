"""QueryRewriter 单测：消解指代 + 无历史跳过 + 失败回退。

用 __new__ 构造并注入桩模型，避免依赖真实 DashScope LLM。
"""
import os
import sys
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from agent.agents.query_rewriter import QueryRewriter
except ModuleNotFoundError:
    from agents.query_rewriter import QueryRewriter


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeModel:
    """可注入的 LLM 桩：invoke 返回预设内容，或抛异常。"""

    def __init__(self, content=None, raises=None):
        self.content = content
        self.raises = raises
        self.invoked = False

    def invoke(self, messages):
        self.invoked = True
        if self.raises:
            raise self.raises
        return _FakeResponse(self.content)


def _make_rewriter(content=None, raises=None):
    """跳过 __init__（不触达 get_chat_model），注入桩模型。"""
    r = QueryRewriter.__new__(QueryRewriter)
    r.user_id = None
    r.model = _FakeModel(content=content, raises=raises)
    return r


class QueryRewriterTests(unittest.TestCase):
    def test_rewrite_resolves_coreference(self):
        r = _make_rewriter(content="分析销售数据的月度趋势")
        history = [
            {"role": "user", "content": "我们最近在看的销售数据"},
            {"role": "assistant", "content": "这是本季度销售数据概览。"},
        ]
        out = r.rewrite("分析它的趋势", history)
        self.assertEqual(out, "分析销售数据的月度趋势")
        self.assertTrue(r.model.invoked)

    def test_rewrite_no_history_returns_unchanged_without_llm(self):
        r = _make_rewriter(content="不应被调用")
        out = r.rewrite("分析月度趋势", [])
        self.assertEqual(out, "分析月度趋势")
        self.assertFalse(r.model.invoked)  # 无历史不调用 LLM

    def test_rewrite_falls_back_on_llm_failure(self):
        r = _make_rewriter(raises=RuntimeError("LLM down"))
        out = r.rewrite("分析它的趋势", [{"role": "user", "content": "销售数据"}])
        self.assertEqual(out, "分析它的趋势")  # 回退原始 query

    def test_rewrite_unchanged_when_llm_echoes_query(self):
        r = _make_rewriter(content="分析它的趋势")
        out = r.rewrite("分析它的趋势", [{"role": "user", "content": "销售数据"}])
        self.assertEqual(out, "分析它的趋势")  # LLM 认为无需改写

    def test_rewrite_empty_query_returned_as_is(self):
        r = _make_rewriter(content="x")
        self.assertEqual(r.rewrite("", [{"role": "user", "content": "x"}]), "")


if __name__ == "__main__":
    unittest.main()
