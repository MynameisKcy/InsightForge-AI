"""embedding 分批入库（add_documents_batched / _ingest_file）离线单测。

背景（fix(vector-store) 8375e75）：DashScope embedding 单批输入上限 20
（batch>20 直接 400 InvalidParameter），_ingest_file 改经 add_documents_batched
客户端分批。此前该路径仅被 live-gated rag_eval fixture 覆盖（re-review Finding 1）。

本文件钉死三个契约，全程离线（RecordingChroma 记录调用、内存 md5 桩，
零 Chroma/DashScope 依赖）：
1. 边界：45 分片 -> 恰好 [20, 20, 5] 三次调用，拼接后与输入逐位一致
   （无丢弃 / 重复 / 乱序）；
2. 等价性：≤20 分片（8 个）-> 恰好 1 次调用、载荷原样——与改造前单次
   add_documents 行为完全等价；
3. 监听器整表语义：_ingest_file 入库全程只触发一次 chunks_added 回调，
   且携带完整分片整表（full-list-once，而非按批次增量拆发）。
"""
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag.vector_store import VectorStoreService


class RecordingChroma:
    """只记录 add_documents 调用的假 Chroma：calls[i] = 第 i+1 批的分片列表。"""

    def __init__(self):
        self.calls: list[list] = []

    def add_documents(self, docs):
        self.calls.append(list(docs))


def _fingerprint(docs):
    """批量写入路径的可比较指纹：逐位的 (page_content, metadata) 元组序列。"""
    return [(d.page_content, dict(d.metadata)) for d in docs]


def _make_service(chunk_size: int = 500) -> VectorStoreService:
    """__new__ 接缝构造（沿用 test_knowledge_selfheal 的 FakeChroma 注入惯例）：
    注入 RecordingChroma + 固定参数真分片器 + 内存 md5，绕开真实持久化与 embedding。
    实例级写回调列表显式置空（类级兜底是不可变空元组，不可 append）。"""
    svc = VectorStoreService.__new__(VectorStoreService)
    svc.vector_store = RecordingChroma()
    svc.spliter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=0,
        separators=["\n\n", ""], length_function=len,
    )
    svc._chunks_added_listeners = []
    svc._source_deleted_listeners = []
    svc._reindexed_listeners = []
    svc._md5_store = set()
    svc._check_md5 = lambda md5: md5 in svc._md5_store
    svc._add_md5 = lambda md5: svc._md5_store.add(md5)
    svc._remove_md5 = lambda md5: svc._md5_store.discard(md5)
    svc._load_md5_store = lambda: set(svc._md5_store)
    return svc


def _docs(n: int) -> list[Document]:
    return [Document(page_content=f"chunk-{i}", metadata={"idx": i}) for i in range(n)]


def test_batch_boundaries_45_chunks_yield_20_20_5():
    """45 分片：恰按 [20, 20, 5] 三批落库，拼接载荷与输入逐位一致。"""
    svc = _make_service()
    docs = _docs(45)

    svc.add_documents_batched(docs)

    assert [len(c) for c in svc.vector_store.calls] == [20, 20, 5]
    flattened = [d for batch in svc.vector_store.calls for d in batch]
    assert _fingerprint(flattened) == _fingerprint(docs)


def test_within_limit_single_call_equivalent_to_legacy_behavior():
    """8 分片（≤20）：恰好 1 次 add_documents 且载荷原样——与改造前单次调用等价。"""
    svc = _make_service()
    docs = _docs(8)

    svc.add_documents_batched(docs)

    assert len(svc.vector_store.calls) == 1
    assert _fingerprint(svc.vector_store.calls[0]) == _fingerprint(docs)


def test_ingest_file_notifies_chunks_listener_full_list_once(tmp_path):
    """>20 分片文件走完整 _ingest_file：回调整表一次性通知，内容=各批次拼接。

    文件用无换行循环数字串 + chunk_size=10/overlap=0，恰好切出 45 个互异
    （数字窗唯一）等长分片——丢片/重复/乱序均可被逐位比对暴露。
    """
    svc = _make_service(chunk_size=10)
    fpath = tmp_path / "kb.txt"
    fpath.write_text("".join(str(i % 10) for i in range(450)), encoding="utf-8")

    received: list[list] = []
    svc.add_chunks_listener(received.append)

    n = svc._ingest_file(str(fpath), md5_hex="deadbeef00", user_id="alice")

    assert n == 45
    # 入库本身仍按 [20, 20, 5] 分批
    assert [len(c) for c in svc.vector_store.calls] == [20, 20, 5]
    # 回调整表语义：只发一次、携带全部 45 片，逐位等于各批次拼接近似写库流
    assert len(received) == 1
    batches_joined = [d for batch in svc.vector_store.calls for d in batch]
    assert _fingerprint(received[0]) == _fingerprint(batches_joined)
    # 整表分片的 owner/md5 元数据齐备（full-list 即最终落库形态）
    assert all(d.metadata["user_id"] == "alice" and d.metadata["file_md5"] == "deadbeef00"
               for d in received[0])
