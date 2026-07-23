"""知识库入库自愈逻辑测试。

复现并验证修复：md5 存储与 chroma 实际状态偏离（md5 在、chroma 空）时，
"已入库"显示但大模型读不到的 bug。
"""
import os
import sys
import tempfile
import unittest

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from agent.rag.vector_store import VectorStoreService
except ModuleNotFoundError:
    from rag.vector_store import VectorStoreService


class FakeChroma:
    """内存版 Chroma：模拟 add_documents / get(where=) / delete(where=|ids=)。"""

    def __init__(self):
        self.docs = []  # [{id, content, metadata}]
        self._ctr = 0

    def add_documents(self, docs):
        for d in docs:
            self._ctr += 1
            self.docs.append({
                "id": f"id{self._ctr}",
                "content": d.page_content,
                "metadata": dict(d.metadata),
            })

    @staticmethod
    def _match(d, where):
        return all(d["metadata"].get(k) == v for k, v in (where or {}).items())

    def get(self, where=None, ids=None):
        out = list(self.docs)
        if where:
            out = [d for d in out if self._match(d, where)]
        if ids is not None:
            idset = set(ids)
            out = [d for d in out if d["id"] in idset]
        return {
            "ids": [d["id"] for d in out],
            "metadatas": [d["metadata"] for d in out],
            "embeddings": [None] * len(out),
        }

    def delete(self, where=None, ids=None):
        if ids is not None:
            idset = set(ids)
            self.docs = [d for d in self.docs if d["id"] not in idset]
        elif where:
            self.docs = [d for d in self.docs if not self._match(d, where)]

    def clear(self):
        self.docs = []


def _make_service():
    """构造未初始化的 VectorStoreService，注入 FakeChroma + 内存 md5 + 真分片器，
    避免依赖真实 Chroma 持久化与 DashScope embedding。"""
    svc = VectorStoreService.__new__(VectorStoreService)
    svc.vector_store = FakeChroma()
    svc.spliter = RecursiveCharacterTextSplitter(
        chunk_size=500, chunk_overlap=50,
        separators=["\n\n", "\n", "。", ".", " "],
    )
    svc._md5_store = set()
    svc._check_md5 = lambda md5: md5 in svc._md5_store
    svc._add_md5 = lambda md5: svc._md5_store.add(md5)
    svc._remove_md5 = lambda md5: svc._md5_store.discard(md5)
    svc._load_md5_store = lambda: set(svc._md5_store)
    return svc


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class KnowledgeSelfHealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kb_test_")
        self.path = os.path.join(self.tmp, "doc.txt")

    def test_ingest_then_skip_no_duplicate(self):
        svc = _make_service()
        _write(self.path, "青岛科技大学研究生参加国际学术会议资助办法，上限8000元。" * 3)
        n, skipped = svc.load_single_document(self.path)
        self.assertGreater(n, 0)
        self.assertFalse(skipped)
        # 再次调用：md5 命中且 chroma 有分片 -> 跳过，无重复分片
        n2, skipped2 = svc.load_single_document(self.path)
        self.assertEqual(n2, 0)
        self.assertTrue(skipped2)
        self.assertEqual(len(svc.vector_store.docs), n)

    def test_self_heal_after_divergence(self):
        """核心 bug：md5 在但 chroma 被清空（偏离）-> load_single_document 自愈重灌。"""
        svc = _make_service()
        _write(self.path, "资助办法规定国际学术会议资助上限8000元。" * 3)
        n, _ = svc.load_single_document(self.path)
        self.assertGreater(n, 0)
        # 模拟偏离：chroma 被清空，md5 仍在
        svc.vector_store.clear()
        self.assertEqual(len(svc._md5_store), 1)
        self.assertFalse(svc._source_has_chunks(self.path))
        self.assertEqual(svc.chroma_sources(), set())
        # 修复前：旧逻辑因 md5 命中直接 skip，永久读不到；修复后：自愈重灌
        n2, skipped2 = svc.load_single_document(self.path)
        self.assertGreater(n2, 0)
        self.assertFalse(skipped2)
        self.assertTrue(svc._source_has_chunks(self.path))
        self.assertIn(self.path, svc.chroma_sources())

    def test_chroma_sources_reflects_actual_state(self):
        svc = _make_service()
        _write(self.path, "内容A" * 200)
        svc.load_single_document(self.path)
        self.assertEqual(svc.chroma_sources(), {self.path})
        svc.vector_store.clear()
        self.assertEqual(svc.chroma_sources(), set())

    def test_content_change_no_duplicate(self):
        """同文件名不同内容（md5 变）：清旧分片后重灌，无重复叠加。"""
        svc = _make_service()
        _write(self.path, "原始内容A，关于学术会议资助。" * 3)
        svc.load_single_document(self.path)
        _write(self.path, "全新内容B，关于差旅报销办法。" * 3)
        n2, skipped2 = svc.load_single_document(self.path)
        self.assertGreater(n2, 0)
        self.assertFalse(skipped2)
        # 仅剩新内容分片（计数 == 新内容分片数，非旧+新叠加）
        expected = svc.spliter.split_documents(
            [Document(page_content="全新内容B，关于差旅报销办法。" * 3,
                      metadata={"source": self.path})])
        self.assertEqual(len(svc.vector_store.docs), len(expected))

    def test_ingest_failure_clears_stale_md5(self):
        """入库失败但 md5 残留（历史脏数据）：清掉误导性 md5，避免假已入库。"""
        svc = _make_service()
        _write(self.path, "x" * 100)
        svc._md5_store.add("stale_md5")  # md5 在
        svc._ingest_file = lambda path, md5_hex=None: 0  # 模拟入库失败
        self.assertNotIn("stale_md5", svc.chroma_sources())  # chroma 无分片
        n, skipped = svc._ingest_if_needed(self.path, "stale_md5")
        self.assertEqual(n, 0)
        self.assertFalse(skipped)
        self.assertNotIn("stale_md5", svc._md5_store)  # 误导性 md5 已清


if __name__ == "__main__":
    unittest.main()
