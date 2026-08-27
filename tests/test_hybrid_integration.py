"""写回调 ↔ BM25 索引同步的离线集成测试。

真 Chroma（tmp 目录 + 唯一 collection）+ 假 embedding（离线），
验证 入库自动入索引 / 删除自动出索引 / 回调异常不影响主流程。
"""
import uuid

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

from rag.bm25 import BM25Index
from rag.vector_store import VectorStoreService


@pytest.fixture()
def vs(tmp_path, monkeypatch):
    fake = DeterministicFakeEmbedding(size=8)
    monkeypatch.setattr("rag.vector_store.get_embed_model", lambda: fake)
    yield VectorStoreService(
        collection_name=f"hybrid_it_{uuid.uuid4().hex[:8]}",
        persist_directory=str(tmp_path),
    )


def _write_kb(tmp_path):
    f = tmp_path / "kb.txt"
    f.write_text(
        "云帆CRM标准版价格是每席每月299元。\n旗舰版支持私有化部署，SLA达到99.99%。\n",
        encoding="utf-8",
    )
    return str(f)


def _wire(vs):
    idx = BM25Index(vs)
    idx.rebuild_from_store()
    vs.add_chunks_listener(idx.add_chunks)
    vs.add_source_deleted_listener(idx.remove_source)
    return idx


def test_ingest_auto_feeds_bm25(vs, tmp_path):
    fpath = _write_kb(tmp_path)
    idx = _wire(vs)
    chunks, _skipped = vs.load_single_document(fpath, "alice")
    assert chunks > 0
    hits = idx.search("299", k=5, viewer_id="alice")
    assert hits
    assert all(h.metadata.get("user_id") in ("alice", "system") for h in hits)


def test_delete_auto_purges_bm25(vs, tmp_path):
    fpath = _write_kb(tmp_path)
    idx = _wire(vs)
    vs.load_single_document(fpath, "alice")
    vs.delete_by_source(fpath, "alice")
    assert idx.search("299", k=5, viewer_id="alice") == []


def test_listener_exception_does_not_break_ingest(vs, tmp_path):
    vs.add_chunks_listener(lambda docs: 1 / 0)
    fpath = _write_kb(tmp_path)
    chunks, _skipped = vs.load_single_document(fpath, "alice")
    assert chunks > 0
