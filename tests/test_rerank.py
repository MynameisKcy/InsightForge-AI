import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rag.rerank import rerank_docs


class _Doc:
    """Document 替身：rerank_docs 只读 page_content / 写 metadata。"""
    def __init__(self, content):
        self.page_content = content
        self.metadata = {}


class _Resp:
    """TextReRank.call 返回替身。"""
    def __init__(self, status_code, results=None, code="", message=""):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.output = {"results": results} if results is not None else None


class RerankDocsTests(unittest.TestCase):
    def test_empty_docs_returns_empty(self):
        self.assertEqual(rerank_docs("q", [], 3, "gte-rerank-v2", 0.0), [])

    def test_few_docs_early_exit_no_api_call(self):
        docs = [_Doc("a"), _Doc("b")]
        with patch("dashscope.TextReRank") as TR:
            out = rerank_docs("q", docs, top_n=5, model="m", score_threshold=0.0)
        self.assertEqual(out, docs)  # 不足 top_n，原样返回
        TR.call.assert_not_called()  # 早退，未调 API

    def test_success_filters_by_threshold_and_writes_score(self):
        docs = [_Doc(f"d{i}") for i in range(4)]
        results = [{"index": 2, "relevance_score": 0.9},
                   {"index": 0, "relevance_score": 0.5}]
        with patch("dashscope.TextReRank") as TR:
            TR.call.return_value = _Resp(200, results=results)
            out = rerank_docs("q", docs, top_n=2, model="m", score_threshold=0.7)
        # 阈值 0.7：仅 index2(0.9) 入选；index0(0.5) 丢弃
        self.assertEqual([d.page_content for d in out], ["d2"])
        self.assertEqual(out[0].metadata["rerank_score"], 0.9)
        args, kwargs = TR.call.call_args
        self.assertEqual(kwargs["model"], "m")
        self.assertEqual(kwargs["top_n"], 2)

    def test_status_not_200_falls_back_to_coarse_top_n(self):
        docs = [_Doc(f"d{i}") for i in range(4)]
        with patch("dashscope.TextReRank") as TR:
            TR.call.return_value = _Resp(500)
            out = rerank_docs("q", docs, top_n=2, model="m", score_threshold=0.0)
        self.assertEqual([d.page_content for d in out], ["d0", "d1"])

    def test_exception_falls_back_to_coarse_top_n(self):
        docs = [_Doc(f"d{i}") for i in range(4)]
        with patch("dashscope.TextReRank") as TR:
            TR.call.side_effect = RuntimeError("boom")
            out = rerank_docs("q", docs, top_n=2, model="m", score_threshold=0.0)
        self.assertEqual([d.page_content for d in out], ["d0", "d1"])


if __name__ == "__main__":
    unittest.main()
