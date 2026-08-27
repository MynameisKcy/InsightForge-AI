import unittest
from unittest.mock import patch

from langchain_core.documents import Document

from rag.rag_service import RagSummarizerService
from utils.config_handler import chroma_conf
from utils.path_tool import get_abs_path
from utils.prompt_loader import load_rag_prompts
from utils.report_exporter import build_report_filename, is_report_content, to_markdown_bytes


class DummyRetrieverWithInvoke:
    def invoke(self, query: str):
        return [Document(page_content=f"{query} 的参考内容", metadata={"source": "unit-test"})]


class DummyRetrieverWithLegacyMethod:
    def get_relevant_documents(self, query: str):
        return [Document(page_content=f"legacy::{query}", metadata={"source": "legacy"})]


class DummyVectorStore:
    """桩向量库：similarity_search 经注入的 retriever 返回文档，避免依赖真实 Chroma。"""
    def __init__(self, retriever):
        self._retriever = retriever

    def get(self, include=None):
        # Hybrid 装配（hybrid_enabled 默认开）：__init__ 会 BM25Index(stub).rebuild_from_store()
        return {"ids": [], "documents": [], "metadatas": []}

    def get_retriver(self, user_id=None, k=None):
        return self._retriever

    def similarity_search(self, query, user_id=None, k=None):
        if hasattr(self._retriever, "invoke"):
            return self._retriever.invoke(query)
        if hasattr(self._retriever, "get_relevant_documents"):
            return self._retriever.get_relevant_documents(query)
        return []


def _make_service(retriever):
    """用 __new__ 构造未初始化的 service，注入桩 vector_store 与 rerank 配置，
    使 _coarse_retrieve / _rerank 不依赖真实 Chroma 与 DashScope。
    （service 本身已不持有 retriever 属性——检索走 vector_store.similarity_search。）"""
    service = RagSummarizerService.__new__(RagSummarizerService)
    service.vector_store = DummyVectorStore(retriever)
    # _coarse_retrieve 用 retrieve_k 覆盖 search_kwargs
    service.retrieve_k = 15
    # _rerank 当 docs 数 <= rerank_top_n 时直接截断返回，不调 DashScope
    service.rerank_top_n = 3
    service.rerank_score_threshold = 0.3
    # Hybrid 关（__new__ 绕过 __init__）：_coarse_pool 走纯 dense 等价路径
    service._bm25 = None
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


class FormatContextBlockTests(unittest.TestCase):
    """上下文块格式化契约：生产 rag_summarize 与 ragas 评估共用的唯一实现
    （架构评审 R2 候选8——评估锁步生产 prompt，生产一改格式评估自动跟随）。"""

    def test_formats_docs_with_numbering_content_and_metadata(self):
        from rag.rag_service import format_context_block

        docs = [
            Document(page_content="甲内容", metadata={"source": "a.txt"}),
            Document(page_content="乙内容", metadata={}),
        ]

        self.assertEqual(
            format_context_block(docs),
            "[参考资料1] 内容:甲内容 | 元数据:{'source': 'a.txt'}\n"
            "[参考资料2] 内容:乙内容 | 元数据:{}\n",
        )

    def test_empty_docs_returns_empty_string(self):
        from rag.rag_service import format_context_block

        self.assertEqual(format_context_block([]), "")


class ConstructorInjectionTests(unittest.TestCase):
    """构造期注入接缝（替代 conftest 的 monkeypatch+__new__ 隔离手法）。"""

    def test_rag_summarizer_uses_injected_vector_store_and_skips_default(self):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        stub_store = DummyVectorStore(DummyRetrieverWithInvoke())
        # 链组装 prompt_template | model 要求 Runnable，用零网络假模型
        with patch("rag.rag_service.get_chat_model",
                   return_value=FakeListChatModel(responses=["ok"])), \
             patch("rag.rag_service.VectorStoreService",
                   side_effect=AssertionError("注入生效时不应构造默认向量库")):
            service = RagSummarizerService(vector_store=stub_store)

        self.assertIs(service.vector_store, stub_store)

    def test_vector_store_persist_directory_param_overrides_config_default(self):
        import rag.vector_store as vs_mod

        recorded = {}

        class RecordingChroma:
            def __init__(self, **kwargs):
                recorded.update(kwargs)

        with patch.object(vs_mod, "Chroma", RecordingChroma), \
             patch.object(vs_mod, "get_embed_model", return_value=object()):
            vs_mod.VectorStoreService(collection_name="ut_iso_a", persist_directory="/tmp/iso_x")
            self.assertEqual(recorded["persist_directory"], "/tmp/iso_x")

            vs_mod.VectorStoreService(collection_name="ut_iso_b")
            self.assertEqual(recorded["persist_directory"],
                             get_abs_path(chroma_conf["persist_directory"]))


class _ScriptedVectorStore:
    """按 query 返回预设文档的桩向量库，用于测试多查询合并去重。"""

    def __init__(self, mapping):
        self._mapping = mapping  # query -> list[Document]

    def similarity_search(self, query, user_id=None, k=None):
        return list(self._mapping.get(query, []))


class _FakeRewriter:
    """跳过 LLM 的检索改写器桩：expand 原样返回预设查询列表。"""

    def __init__(self, queries):
        self._queries = queries

    def expand(self, query, user_id=None):
        return list(self._queries)


class RetrieverMultiQueryTests(unittest.TestCase):
    """多查询扩展在 retriever_docs 中的合并/去重/回退（不触达 DashScope）。"""

    def _make(self, mapping, rewriter=None):
        service = RagSummarizerService.__new__(RagSummarizerService)
        service.vector_store = _ScriptedVectorStore(mapping)
        service.retrieve_k = 15
        service.rerank_top_n = 3
        service.rerank_score_threshold = 0.3
        # Hybrid 关（__new__ 绕过 __init__）：_coarse_pool 走纯 dense 等价路径
        service._bm25 = None
        if rewriter is not None:
            service._query_rewriter = rewriter
        return service

    def test_multi_query_unions_and_dedups(self):
        doc1 = Document(page_content="销售数据A", metadata={"source": "a.txt"})
        doc2 = Document(page_content="销售数据B", metadata={"source": "a.txt"})
        doc3 = Document(page_content="利润数据C", metadata={"source": "b.txt"})
        mapping = {
            "扫地机器人": [doc1, doc2],
            "改写A": [doc2, doc3],   # doc2 与原始重复 -> 去重
            "改写B": [doc1],          # doc1 重复 -> 去重
        }
        service = self._make(mapping, rewriter=_FakeRewriter(["扫地机器人", "改写A", "改写B"]))
        docs = service.retriever_docs("扫地机器人")
        # 3 个唯一 chunk；_rerank 在 docs<=top_n 时直接截断返回，不调 DashScope
        self.assertEqual(len(docs), 3)
        contents = {d.page_content for d in docs}
        self.assertEqual(contents, {"销售数据A", "销售数据B", "利润数据C"})
        # doc3 只能从改写A召回 -> 证明多查询确实扩大了召回
        self.assertIn("利润数据C", contents)

    def test_no_rewriter_falls_back_to_single_query(self):
        doc1 = Document(page_content="销售数据A", metadata={"source": "a.txt"})
        mapping = {"扫地机器人": [doc1]}
        service = self._make(mapping, rewriter=None)  # 不注入改写器
        docs = service.retriever_docs("扫地机器人")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].page_content, "销售数据A")


if __name__ == "__main__":
    unittest.main()
