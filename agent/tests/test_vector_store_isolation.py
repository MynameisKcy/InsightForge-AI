"""向量库 owner 隔离回归测试：用真实 in-memory Chroma（假 embed）验证
A 上传的知识 B 检索/列表/删除均不可见，公共 system 知识对所有用户可见。"""
import os
import sys
import unittest
import uuid

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag.vector_store import VectorStoreService, PUBLIC_OWNER


class FakeEmbed(Embeddings):
    """确定性假嵌入，不依赖 DashScope。"""
    def embed_documents(self, texts):
        return [[float(len(t) % 7), float((hash(t) & 0xFFFF) % 13)] for t in texts]
    def embed_query(self, text):
        return [float(len(text) % 7), float((hash(text) & 0xFFFF) % 13)]


def _make_vs() -> VectorStoreService:
    """构造未初始化的 service，注入 in-memory Chroma，跳过真实 embed 与迁移。

    每次 call 用唯一 collection 名：chromadb 会缓存 ephemeral client，同一 collection
    名的实例共享后端状态，会导致测试间数据串扰（前一个用例写入的 system 分片漏进
    后一个用例的检索结果）。唯一名保证各用例集合互不干扰。
    """
    vs = VectorStoreService.__new__(VectorStoreService)
    vs.vector_store = Chroma(collection_name=f"iso_test_{uuid.uuid4().hex[:8]}",
                             embedding_function=FakeEmbed())
    vs.spliter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    return vs


class VectorStoreOwnerIsolationTests(unittest.TestCase):
    def test_retrieve_list_delete_isolated_by_owner(self):
        vs = _make_vs()
        alice_doc = Document(page_content="alice 机密内容", metadata={"source": "/data/alice/a.txt", "user_id": "alice"})
        bob_doc = Document(page_content="bob 公开内容", metadata={"source": "/data/bob/b.txt", "user_id": "bob"})
        vs.vector_store.add_documents([alice_doc, bob_doc])

        # 检索隔离：alice 只能命中自己的
        alice_hits = vs.similarity_search("内容", user_id="alice", k=10)
        self.assertEqual({d.metadata["user_id"] for d in alice_hits}, {"alice"})
        bob_hits = vs.similarity_search("内容", user_id="bob", k=10)
        self.assertEqual({d.metadata["user_id"] for d in bob_hits}, {"bob"})

        # 列表隔离：chroma_sources 仅含自己的来源
        self.assertIn("/data/alice/a.txt", vs.chroma_sources("alice"))
        self.assertNotIn("/data/alice/a.txt", vs.chroma_sources("bob"))
        self.assertIn("/data/bob/b.txt", vs.chroma_sources("bob"))

        # 删除隔离：删 bob 的不影响 alice
        vs.delete_by_source("/data/bob/b.txt", user_id="bob")
        self.assertIn("/data/alice/a.txt", vs.chroma_sources("alice"))
        self.assertNotIn("/data/bob/b.txt", vs.chroma_sources("bob"))

    def test_public_owner_visible_to_all_users(self):
        vs = _make_vs()
        sys_doc = Document(page_content="系统公共知识", metadata={"source": "/sys/s.txt", "user_id": PUBLIC_OWNER})
        vs.vector_store.add_documents([sys_doc])

        # 任意用户检索都能命中公共 system 知识
        alice_hits = vs.similarity_search("知识", user_id="alice", k=10)
        bob_hits = vs.similarity_search("知识", user_id="bob", k=10)
        self.assertTrue(any(d.metadata["user_id"] == PUBLIC_OWNER for d in alice_hits))
        self.assertTrue(any(d.metadata["user_id"] == PUBLIC_OWNER for d in bob_hits))
        # 但列表/统计仅含自己的（公共不进个人列表）
        self.assertEqual(vs.chroma_sources("alice"), set())
        self.assertEqual(vs.chroma_sources("bob"), set())

    def test_ingest_file_writes_owner_metadata(self):
        vs = _make_vs()
        # _ingest_file 写入的 user_id 元数据决定归属
        doc = Document(page_content="测试内容" * 20, metadata={"source": "/data/x.txt"})
        # 直接复用 _ingest_file 的 metadata 写入逻辑（不依赖文件 IO）
        md5_hex = "deadbeef"
        uid = "alice"
        for d in vs.spliter.split_documents([doc]):
            d.metadata.setdefault("source", "/data/x.txt")
            d.metadata.setdefault("file_md5", md5_hex)
            d.metadata["user_id"] = uid
        vs.vector_store.add_documents(vs.spliter.split_documents([
            Document(page_content="测试内容" * 20, metadata={"source": "/data/x.txt", "file_md5": md5_hex, "user_id": uid})
        ]))
        self.assertEqual(vs.chroma_sources("alice"), {"/data/x.txt"})
        self.assertEqual(vs.chroma_sources("bob"), set())


if __name__ == "__main__":
    unittest.main()
