"""RAG 评估共享 fixture：隔离的 RagSummarizerService（真实 embed + 真实检索/生成链路）。

隔离手法沿用 tests/test_vector_store_isolation.py 的 __new__ 注入惯例：
patch rag_service.VectorStoreService 为临时工厂（唯一 collection + 临时持久化目录），
再正常构造 RagSummarizerService —— prompt/rerank/改写器等 __init__ 逻辑全部走真实路径，
仅向量库指向评估专用存储，不污染用户真实 chroma_db。

运行条件：DASHSCOPE_API_KEY 有效（embedding/rerank/LLM 都要真实调用）。
默认跳过（pytest.ini addopts -m "not rag_eval"），显式运行：
    cd agent && python -m pytest tests/rag_eval -m rag_eval -v
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

try:
    from agent.rag.rag_service import RagSummarizerService
    from agent.rag.vector_store import VectorStoreService
    from agent.model.factory import get_embed_model
    import agent.rag.rag_service as _rs_module
except ModuleNotFoundError:
    from rag.rag_service import RagSummarizerService
    from rag.vector_store import VectorStoreService
    from model.factory import get_embed_model
    import rag.rag_service as _rs_module

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

    def _isolated_vss():
        vs = VectorStoreService.__new__(VectorStoreService)
        vs.vector_store = Chroma(
            collection_name=collection,
            embedding_function=get_embed_model(),
            persist_directory=str(persist_dir),
        )
        vs.spliter = RecursiveCharacterTextSplitter(
            chunk_size=500, chunk_overlap=50,
            separators=["\n\n", "\n", "。", ".", ",", "!", "?", " ", ""],
        )
        return vs

    orig_vss = _rs_module.VectorStoreService
    _rs_module.VectorStoreService = _isolated_vss
    try:
        svc = RagSummarizerService()
    finally:
        _rs_module.VectorStoreService = orig_vss   # 立即恢复，只影响本次构造

    # ingest 受控语料（chunk 继承 source 元数据中的 user_id=EVAL_USER）
    corpus = Path(CURRENT_DIR) / "eval_knowledge.md"
    docs = [Document(page_content=corpus.read_text(encoding="utf-8"),
                     metadata={"source": str(corpus), "user_id": EVAL_USER})]
    store = svc.vector_store
    store.vector_store.add_documents(store.spliter.split_documents(docs))
    return svc


@pytest.fixture(scope="session")
def retrieved_contexts(rag_service) -> dict[str, list[str]]:
    """对全部用例跑一次真实检索（会话级缓存，避免重复 DashScope 调用）。"""
    return {
        case["question"]: [d.page_content for d in rag_service.retriever_docs(case["question"], user_id=EVAL_USER)]
        for case in TEST_CASES
    }
