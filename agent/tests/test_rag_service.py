import os
import sys
import unittest

from langchain_core.documents import Document

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if PROJECT_PARENT not in sys.path:
    sys.path.insert(0, PROJECT_PARENT)

try:
    from agent.rag.rag_service import RagSummarizerService
    from agent.utils.prompt_loader import load_rag_prompts
    from agent.utils.report_exporter import build_report_filename, is_report_content, to_markdown_bytes
except ModuleNotFoundError:
    from rag.rag_service import RagSummarizerService
    from utils.prompt_loader import load_rag_prompts
    from utils.report_exporter import build_report_filename, is_report_content, to_markdown_bytes


class DummyRetrieverWithInvoke:
    def invoke(self, query: str):
        return [Document(page_content=f"{query} 的参考内容", metadata={"source": "unit-test"})]


class DummyRetrieverWithLegacyMethod:
    def get_relevant_documents(self, query: str):
        return [Document(page_content=f"legacy::{query}", metadata={"source": "legacy"})]


class DummyVectorStore:
    """桩向量库：get_retriver 返回注入的 retriever，避免依赖真实 Chroma。"""
    def __init__(self, retriever):
        self._retriever = retriever

    def get_retriver(self):
        return self._retriever


def _make_service(retriever):
    """用 __new__ 构造未初始化的 service，注入桩 vector_store 与 rerank 配置，
    使 _coarse_retrieve / _rerank 不依赖真实 Chroma 与 DashScope。"""
    service = RagSummarizerService.__new__(RagSummarizerService)
    service.retriever = retriever
    service.vector_store = DummyVectorStore(retriever)
    # _coarse_retrieve 用 retrieve_k 覆盖 search_kwargs
    service.retrieve_k = 15
    # _rerank 当 docs 数 <= rerank_top_n 时直接截断返回，不调 DashScope
    service.rerank_top_n = 3
    service.rerank_score_threshold = 0.3
    return service


class RagServiceTests(unittest.TestCase):
    def test_load_rag_prompts_reads_existing_file(self):
        prompt_text = load_rag_prompts()
        self.assertIn("{input}", prompt_text)
        self.assertIn("{context}", prompt_text)

    def test_retriever_docs_prefers_invoke(self):
        service = _make_service(DummyRetrieverWithInvoke())

        docs = service.retriever_docs("扫地机器人")

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].page_content, "扫地机器人 的参考内容")

    def test_retriever_docs_supports_legacy_langchain_api(self):
        service = _make_service(DummyRetrieverWithLegacyMethod())

        docs = service.retriever_docs("小户型")

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].page_content, "legacy::小户型")

    def test_rag_summarize_formats_context(self):
        service = _make_service(DummyRetrieverWithInvoke())

        captured = {}

        class DummyChain:
            def invoke(self, payload):
                captured.update(payload)
                return "summary"

        service.chain = DummyChain()

        result = service.rag_summarize("小户型适合哪些扫地机器人")

        self.assertIn("summary", result)
        self.assertIn("## 参考来源", result)
        self.assertIn("unit-test", result)
        self.assertEqual(captured["input"], "小户型适合哪些扫地机器人")
        self.assertIn("[参考资料1] 内容:小户型适合哪些扫地机器人 的参考内容", captured["context"])
        self.assertIn("元数据:{'source': 'unit-test'}", captured["context"])

    def test_format_reference_sources_includes_file_page_and_excerpt(self):
        service = RagSummarizerService.__new__(RagSummarizerService)
        docs = [
            Document(
                page_content="机器人使用前需要清理地面线缆并确认传感器无遮挡。",
                metadata={"source": "D:/docs/robot_safety.pdf", "page": 2},
            )
        ]

        references = service.format_reference_sources(docs)

        self.assertIn("## 参考来源", references)
        self.assertIn("robot_safety.pdf", references)
        self.assertIn("第3页", references)
        self.assertIn("机器人使用前需要清理地面线缆", references)

    def test_report_exporter_detects_report_and_builds_markdown_file(self):
        content = """# 用户使用情况报告

## 基本信息
用户 ID:1001，月份:5

## 使用概况
常用夜间静音清扫。

## 效率表现
效率为87%。

## 建议
建议定期维护边刷。
"""

        self.assertTrue(is_report_content(content))
        self.assertTrue(build_report_filename(content).startswith("user_report_1001_5_"))
        self.assertEqual(to_markdown_bytes(content), content.strip().encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
