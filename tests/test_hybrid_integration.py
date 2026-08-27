"""写回调 ↔ BM25 索引同步的离线集成测试。

真 Chroma（tmp 目录 + 唯一 collection）+ 假 embedding（离线），
验证 入库自动入索引 / 删除自动出索引 / 回调异常不影响主流程。
"""
import uuid

import pytest
from langchain_core.documents import Document
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


def test_selfheal_notify_passes_effective_uid_not_raw(vs, tmp_path):
    """自愈清残通知传 effective uid（"default"）判别单测（T5 评审遗留）。

    既有测试两次都传 user_id="alice"（uid == 原始 user_id），无法区分通知里
    传的是 effective uid 还是裸 user_id。本测试两次都传 None：正确实现必须
    收到 (path, "default")；若误传原始 user_id 则收到 (path, None)。
    """
    received: list[tuple[str, str | None]] = []
    vs.add_source_deleted_listener(lambda source, uid: received.append((source, uid)))

    # 内容各次运行唯一（uuid 内嵌）：防全局 md5 存储跨运行残留命中
    f = tmp_path / "kb_uid.txt"
    f.write_text(f"云帆CRM{uuid.uuid4().hex[:8]}标准版价格是每席每月299元。\n", encoding="utf-8")
    path = str(f)

    n1, _skipped1 = vs.load_single_document(path, None)
    assert n1 > 0
    assert received == []  # 首次入库不走清残分支，不发通知

    # 改内容（md5 变）再 load：触发自愈清残 → 清残分支补发 source_deleted
    f.write_text(f"旗舰版{uuid.uuid4().hex[:8]}支持私有化部署，SLA达到99.99%。\n", encoding="utf-8")
    n2, _skipped2 = vs.load_single_document(path, None)
    assert n2 > 0
    assert received == [(path, "default")]  # 判别点：不是 (path, None)


def test_selfheal_reingest_notifies_bm25_with_effective_uid(vs, tmp_path):
    """自愈清残（内容变更重灌）必须补发 source_deleted 通知，且传 effective uid。

    旧 chunk 经裸 delete 清除时若不通知，BM25 会残留 旧∪新 分片直到全量重建；
    若误传原始 user_id(None)，remove_source 的 None 匹配会过度清除其他 owner
    同名 source 的索引条目——故用 bob 预置条目钉死此处必须传 uid。
    """
    # 内容各次运行唯一（uuid 内嵌）：防全局 md5 存储（跨运行持久）残留命中
    # 使第二次 load 走"已存在跳过"而非本测试靶定的"自愈清残+重灌"分支。
    f = tmp_path / "kb_mutate.txt"
    old_text = f"云帆CRM{uuid.uuid4().hex[:8]}标准版价格是每席每月299元。\n"
    new_text = f"旗舰版{uuid.uuid4().hex[:8]}支持私有化部署，SLA达到99.99%。\n"
    f.write_text(old_text, encoding="utf-8")
    idx = BM25Index(vs)
    idx.rebuild_from_store()
    vs.add_chunks_listener(idx.add_chunks)
    vs.add_source_deleted_listener(idx.remove_source)

    path = str(f)
    n1, _skipped1 = vs.load_single_document(path, "alice")
    assert n1 > 0
    # 其他 owner 挂在同一 source 路径下的预置索引条目：自愈清残不应波及
    idx.add_chunks([Document(
        page_content="BOBX-9000 专属文档。",
        metadata={"source": path, "user_id": "bob"},
    )])
    assert idx.search("299", k=5, viewer_id="alice")
    assert idx.search("9000", k=5, viewer_id="bob")

    f.write_text(new_text, encoding="utf-8")
    n2, skipped = vs.load_single_document(path, "alice")
    assert n2 > 0 and skipped is False

    # 旧唯一术语已随清残通知从 BM25 移除；新术语经入库回调进入索引
    assert idx.search("299", k=5, viewer_id="alice") == []
    new_hits = idx.search("私有化", k=5, viewer_id="alice")
    assert new_hits and all(h.metadata.get("user_id") == "alice" for h in new_hits)
    # effective uid 生效证据：bob 的同 source 条目未被 None 式全源清除
    assert [h for h in idx.search("9000", k=5, viewer_id="bob")]
