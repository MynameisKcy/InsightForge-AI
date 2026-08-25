"""RAG 评估共享 fixture：隔离的 RagSummarizerService（真实 embed + 真实检索/生成链路）。

隔离走构造期注入（评审 R2 候选8）：VectorStoreService(persist_directory=临时目录,
collection_name=唯一名) + RagSummarizerService(vector_store=注入) —— prompt/rerank/
改写器等 __init__ 逻辑全部走真实构造路径，仅向量库指向评估专用存储，
不污染用户真实 chroma_db。唯一 collection 名避免 chromadb ephemeral client
缓存导致用例间数据串扰（同 tests/test_vector_store_isolation.py 的教训）。

运行条件：DASHSCOPE_API_KEY 有效（embedding/rerank/LLM 都要真实调用）。
默认跳过（pytest.ini addopts -m "not rag_eval"），显式运行：
    python -m pytest tests/rag_eval -m rag_eval -v
"""
import os
import uuid
from pathlib import Path

import pytest

from langchain_core.documents import Document

from rag.rag_service import RagSummarizerService
from rag.vector_store import VectorStoreService

from tests.rag_eval.test_cases import TEST_CASES

EVAL_USER = "u_rag_eval"

pytestmark = pytest.mark.rag_eval


@pytest.fixture(scope="session")
def rag_service(tmp_path_factory):
    """构造隔离的 RagSummarizerService 并 ingest 评估语料。"""
    if not os.getenv("DASHSCOPE_API_KEY"):
        pytest.skip("需要 DASHSCOPE_API_KEY（embedding/rerank/LLM 真实调用）")

    persist_dir = tmp_path_factory.mktemp("chroma_rag_eval")
    collection = f"rag_eval_{uuid.uuid4().hex[:8]}"

    vs = VectorStoreService(collection_name=collection, persist_directory=str(persist_dir))
    svc = RagSummarizerService(vector_store=vs)

    # ingest 受控语料（chunk 继承 source 元数据中的 user_id=EVAL_USER）
    corpus = Path(__file__).parent / "eval_knowledge.md"
    docs = [Document(page_content=corpus.read_text(encoding="utf-8"),
                     metadata={"source": str(corpus), "user_id": EVAL_USER})]
    store = svc.vector_store
    store.vector_store.add_documents(store.spliter.split_documents(docs))
    return svc


@pytest.fixture(scope="session")
def retrieved_contexts(rag_service) -> dict[str, list[Document]]:
    """对全部用例跑一次真实检索（会话级缓存，避免重复 DashScope 调用）。

    返回 Document 列表而非裸文本：评估侧经 format_context_block 格式化时
    与生产完全同参（内容 + 元数据都进 prompt）。
    """
    return {
        case["question"]: rag_service.retriever_docs(case["question"], user_id=EVAL_USER)
        for case in TEST_CASES
    }
