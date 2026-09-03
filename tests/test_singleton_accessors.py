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


def test_summarizer_constructor_runs_once_under_concurrency(monkeypatch):
    """双重检查锁定：并发冷启动只构造一次。

    Hybrid 装配后 __init__ 会向共享 store 注册写回调——构造竞态会导致
    回调重复注册且永不摘除，必须与 get_default_vector_store 同款加锁。
    """
    import threading
    import time

    import rag.rag_service as rs

    created = []

    class _Stub:
        def __init__(self):
            # 拉宽构造窗口（sleep 让出 GIL）：无锁时其余线程会在赋值前通过 None 检查、
            # 各自再构造一次；加锁后仍只有持锁者构造。
            time.sleep(0.05)
            created.append(self)

    monkeypatch.setattr(rs, "RagSummarizerService", _Stub)
    reset_default_rag_summarizer()
    try:
        barrier = threading.Barrier(8)
        results = []

        def _worker():
            barrier.wait()
            results.append(get_default_rag_summarizer())

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(created) == 1
        assert all(r is created[0] for r in results)
    finally:
        # 摘掉 stub 单例，避免污染同模块其他用例（teardown_module 兜底二次重置）
        reset_default_rag_summarizer()


def test_reset_breaks_identity():
    first = get_default_vector_store()
    reset_default_vector_store()
    assert first is not get_default_vector_store()


def teardown_module(module):
    reset_default_vector_store()
    reset_default_rag_summarizer()
