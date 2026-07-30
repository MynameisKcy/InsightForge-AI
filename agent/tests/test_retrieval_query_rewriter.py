"""RetrievalQueryRewriter 单测：多查询扩展的解析/去重/限流 + 失败回退。

打桩模块级 get_chat_model，避免依赖真实 DashScope LLM。
"""
import os
import sys
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    import agent.rag.retrieval_query_rewriter as rqr_mod
    from agent.rag.retrieval_query_rewriter import RetrievalQueryRewriter
except ModuleNotFoundError:
    import retrieval_query_rewriter as rqr_mod
    from retrieval_query_rewriter import RetrievalQueryRewriter


class _FakeResponse:
    def __init__(self, content):
        self.content = content


def _patch_model(content=None, raises=None):
    """让 expand 内部取到的 get_chat_model 返回预设桩模型。"""
    class _FakeModel:
        def invoke(self, messages):
            if raises:
                raise raises
            return _FakeResponse(content)

    rqr_mod.get_chat_model = lambda user_id=None: _FakeModel()


class RetrievalQueryRewriterTests(unittest.TestCase):
    def setUp(self):
        self.rw = RetrievalQueryRewriter()

    def test_expand_returns_original_plus_paraphrases(self):
        _patch_model(content="销售趋势分析\n各月销售额变化\n月度营收走势")
        out = self.rw.expand("销售趋势")
        self.assertEqual(out[0], "销售趋势")
        self.assertEqual(len(out), 4)  # 原始 + 3 条改写
        self.assertIn("销售趋势分析", out)

    def test_expand_dedups_and_limits_to_n(self):
        _patch_model(content="改写1\n改写1\n改写2\n改写3\n改写4")
        out = self.rw.expand("query")
        # 去重后 1/2/3，限到 n=3，加原始 -> 4 条
        self.assertEqual(out, ["query", "改写1", "改写2", "改写3"])

    def test_expand_strips_numbering_and_quotes(self):
        _patch_model(content='1. "改写A"\n2. 改写B\n3) 改写C')
        out = self.rw.expand("query")
        self.assertIn("改写A", out)  # 编号与引号被清理
        self.assertIn("改写B", out)
        self.assertIn("改写C", out)

    def test_expand_falls_back_on_llm_failure(self):
        _patch_model(raises=RuntimeError("LLM down"))
        out = self.rw.expand("销售趋势")
        self.assertEqual(out, ["销售趋势"])

    def test_expand_empty_query(self):
        out = self.rw.expand("")
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
