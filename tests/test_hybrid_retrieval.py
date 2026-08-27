"""RRF 融合序、聚合截断、降级与开关等价性的离线单元测试（stub 全依赖，不触网）。"""
from langchain_core.documents import Document

from rag.rag_service import RagSummarizerService


def _d(text):
    return Document(page_content=text, metadata={"source": text})


def _svc(dense, bm25=None, rrf_k=60, bm25_top_k=15):
    svc = RagSummarizerService.__new__(RagSummarizerService)
    svc.retrieve_k = 15
    svc.rrf_k = rrf_k
    svc.bm25_top_k = bm25_top_k
    svc._query_rewriter = None  # expand 回退 [原 query]
    svc._coarse_retrieve = lambda q, user_id=None: list(dense)
    svc._rerank = lambda query, docs: docs  # 直通，便于断言池序
    svc._bm25 = bm25
    return svc


def test_hybrid_off_pool_equals_dense_exactly():
    dense = [_d("A"), _d("B"), _d("C")]
    svc = _svc(dense=dense, bm25=None)
    pool = svc._coarse_pool("任意问题")
    assert [d.page_content for d in pool] == ["A", "B", "C"]


def test_rrf_fuse_known_order():
    dense = [_d("A"), _d("B"), _d("C")]
    bm = [_d("B"), _d("D"), _d("E")]
    fused = RagSummarizerService._rrf_fuse(dense, bm, k=60)
    # B=1/61+1/60 最高 > A=1/60 > D=1/61 > C=E=1/62（并列按先见序 C 前）
    assert [d.page_content for d in fused] == ["B", "A", "D", "C", "E"]


def test_bm25_failure_degrades_to_dense(caplog):
    class Boom:
        def search(self, *a, **kw):
            raise RuntimeError("bm25 down")

    dense = [_d("A"), _d("B")]
    svc = _svc(dense=dense, bm25=Boom())
    pool = svc._coarse_pool("任意问题")
    assert [d.page_content for d in pool] == ["A", "B"]


def test_aggregate_prefers_best_rank_and_truncates():
    hits_by_query = {"q1": [_d("X"), _d("Y"), _d("Z")], "q2": [_d("Y")]}

    class Stub:
        def search(self, q, k, viewer_id):
            return hits_by_query.get(q, [])[:k]

    svc = _svc(dense=[], bm25=Stub(), bm25_top_k=2)
    out = svc._aggregate_bm25(["q1", "q2"], None)
    # Y 在 q2 排名 0 优于 q1 排名 1 → Y 居首；截断 top2
    # （并列同 best_rank 时后取得名次者优先——被更多扩展 query 共同命中的共识文档靠前，
    #   故 Y(经 q2 提升至 rank0) 排在仅单 query rank0 的 X 之前。）
    assert [d.page_content for d in out] == ["Y", "X"]


# ── 装配测试（真实构造，monkeypatch 配置字典）──

class EmptyGetStore:
    def __init__(self):
        self.get_calls = 0

    def get(self, include=None):
        self.get_calls += 1
        return {"ids": [], "documents": [], "metadatas": []}


class ListenerStore(EmptyGetStore):
    """镜像 VectorStoreService 的三张监听表字段名与注册方法，
    供装配测试断言「每张表恰好一条回调」（controller addition C）。"""

    def __init__(self):
        super().__init__()
        self.registered = []
        self._chunks_added_listeners = []
        self._source_deleted_listeners = []
        self._reindexed_listeners = []

    def add_chunks_listener(self, fn):
        self.registered.append(("chunks", fn))
        self._chunks_added_listeners.append(fn)

    def add_source_deleted_listener(self, fn):
        self.registered.append(("deleted", fn))
        self._source_deleted_listeners.append(fn)

    def add_reindexed_listener(self, fn):
        self.registered.append(("reindexed", fn))
        self._reindexed_listeners.append(fn)


def _with_conf(monkeypatch, conf):
    import rag.rag_service as rs
    monkeypatch.setattr(rs, "rag_conf", conf)


def test_wiring_disabled_no_bm25(monkeypatch):
    _with_conf(monkeypatch, {"hybrid_enabled": False})
    svc = RagSummarizerService(vector_store=EmptyGetStore())
    assert svc._bm25 is None
    assert svc.hybrid_enabled is False


def test_wiring_enabled_registers_three_listeners(monkeypatch):
    _with_conf(monkeypatch, {"hybrid_enabled": True})
    store = ListenerStore()
    svc = RagSummarizerService(vector_store=store)
    assert svc._bm25 is not None
    assert {kind for kind, _fn in store.registered} == {"chunks", "deleted", "reindexed"}
    # 三张监听表各恰好一条回调，且指向注入的 BM25 索引（Task4 遗留 minor：reindexed 此前无直接信号）
    assert len(store._chunks_added_listeners) == 1
    assert len(store._source_deleted_listeners) == 1
    assert len(store._reindexed_listeners) == 1
    assert store._chunks_added_listeners[0] == svc._bm25.add_chunks
    assert store._source_deleted_listeners[0] == svc._bm25.remove_source
    # reindexed 回调端到端信号：初始 rebuild 读过一次 store，触发回调应再次全量重建读取
    assert store.get_calls == 1
    store._reindexed_listeners[0]()
    assert store.get_calls == 2


def test_wiring_enabled_tolerates_store_without_listeners(monkeypatch):
    _with_conf(monkeypatch, {"hybrid_enabled": True})
    svc = RagSummarizerService(vector_store=EmptyGetStore())  # 无注册方法也不炸
    assert svc._bm25 is not None
