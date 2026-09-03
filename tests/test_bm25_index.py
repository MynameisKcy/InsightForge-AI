"""BM25Index 与分词探针的离线单元测试（stub store，不触 chroma/DashScope）。

分词探针锁定 Hybrid 靶心行为：数字/型号 token 必须存活（299 / 99.99 / AES-256 /
100MB）——这正是纯稠密检索的弱项，jieba 对它们的切分不可信，靠正则显式提取。
"""
import pytest
from langchain_core.documents import Document

from rag.bm25 import BM25Index, tokenize

# ── 分词探针 ──

@pytest.mark.parametrize("text,token", [
    ("标准版价格是每席每月299元", "299"),
    ("SLA 99.99%，7×24 小时", "99.99"),
    ("采用 AES-256 加密算法", "AES-256"),
    ("单个附件大小上限 100MB", "100MB"),
])
def test_digit_tokens_survive(text, token):
    assert token in tokenize(text)


def test_chinese_words_present():
    toks = tokenize("销售管道最多能配置阶段")
    assert "销售" in toks and "管道" in toks


def test_empty_safe():
    assert tokenize("") == []


# ── 索引行为（stub store） ──

class StubStore:
    """鸭子类型 store：只需实现 .get(include=...)。"""

    def __init__(self, rows):
        self._rows = rows

    def get(self, include=None):
        return {
            "ids": [str(i) for i in range(len(self._rows))],
            "documents": [t for t, _ in self._rows],
            "metadatas": [dict(m) for _, m in self._rows],
        }


def _doc(text, uid="alice", source="s1"):
    return Document(page_content=text, metadata={"user_id": uid, "source": source})


def make_index(entries):
    idx = BM25Index(StubStore([(d.page_content, d.metadata) for d in entries]))
    idx.rebuild_from_store()
    return idx


def test_rebuild_and_keyword_search():
    idx = make_index([_doc("标准版价格是每席每月299元"), _doc("旗舰版支持私有化部署")])
    hits = idx.search("299 价格多少", k=5, viewer_id="alice")
    assert len(hits) >= 1
    assert "299" in hits[0].page_content


def test_owner_filter_viewer_sees_self_and_system_only():
    idx = make_index([
        _doc("alice的文档包含299", uid="alice", source="a"),
        _doc("system公共知识含299", uid="system", source="b"),
        _doc("bob的私有笔记299", uid="bob", source="c"),
    ])
    sources = {h.metadata["source"] for h in idx.search("299", k=10, viewer_id="alice")}
    assert "a" in sources and "b" in sources and "c" not in sources


def test_none_viewer_sees_all():
    idx = make_index([_doc("alice的299", uid="alice"), _doc("bob的299", uid="bob")])
    assert len(idx.search("299", k=10, viewer_id=None)) == 2


def test_add_chunks_incremental():
    idx = make_index([])
    idx.add_chunks([_doc("标准版299元每月")])
    assert len(idx.search("299", k=5, viewer_id="alice")) == 1


def test_remove_source_scoped_by_owner():
    idx = make_index([
        _doc("同一份内容299", uid="alice", source="shared"),
        _doc("同一份内容299", uid="bob", source="shared"),
    ])
    idx.remove_source("shared", "alice")
    left = idx.search("299", k=10, viewer_id=None)
    assert len(left) == 1 and left[0].metadata["user_id"] == "bob"


def test_remove_source_none_owner_removes_all():
    idx = make_index([
        _doc("内容A含299", uid="alice", source="x"),
        _doc("内容B含299", uid="bob", source="x"),
    ])
    idx.remove_source("x", None)
    assert idx.search("299", k=10, viewer_id=None) == []


def test_empty_index_search_safe():
    idx = make_index([])
    assert idx.search("任意查询", k=5, viewer_id="alice") == []
