"""前置任务0（方案一）：生产实例单例统一——三处访问器同实例 + 测试隔离。"""
import api.deps as deps
from rag.rag_service import (
    RagSummarizerService,
    get_default_rag_summarizer,
    reset_default_rag_summarizer,
)
from rag.vector_store import (
    get_default_vector_store,
    reset_default_vector_store,
)


def test_store_accessors_identical():
    assert deps._get_vector_store() is get_default_vector_store()
    svc = RagSummarizerService()
    assert svc.vector_store is get_default_vector_store()


def test_summarizer_is_process_singleton():
    assert get_default_rag_summarizer() is get_default_rag_summarizer()


def test_reset_breaks_identity():
    first = get_default_vector_store()
    reset_default_vector_store()
    assert first is not get_default_vector_store()


def teardown_module(module):
    reset_default_vector_store()
    reset_default_rag_summarizer()
