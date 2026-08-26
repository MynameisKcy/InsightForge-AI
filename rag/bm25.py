"""BM25 词法检索通道（Hybrid 双路之一）：jieba 分词 + 正则数字 token。

与 Chroma 向量粗召回并行的第二路候选来源。进程内内存索引；chroma 是唯一真相，
漂移时 rebuild_from_store() 全量自愈（与 md5 自愈同哲学）。
owner 可见性语义与 VectorStoreService._owner_filter(include_public=True) 一致：
viewer 可见 自己 + 公共 system；viewer_id=None 不过滤（全量，兼容既有约定）。
个人知识库量级下任何变更都全量重训（毫秒级），规避 rank_bm25 不支持增量的问题。
"""
import re
import threading

import jieba
from langchain_core.documents import Document
from rank_bm25 import BM25Okapi

from utils.logger_handler import logger

# 数字/型号 token："299"、"99.99"、"100MB"、"AES-256"——Hybrid 的靶心，
# 不能信任 jieba 对它们的切分，正则显式提取（与 jieba 结果拼接，重复无害）。
# 尾部不收 %/％：查询侧通常不带百分号，token 保持纯数字/字母便于跨侧匹配。
# 字母前缀须带连字符才并入（"AES-256" 整体存活）；纯字母词不误伤（"SLA" 不匹配）。
_DIGIT_TOKEN_RE = re.compile(r"(?:[A-Za-z]+[-_])?\d[\d.\-]*[A-Za-z]*")


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    words = [w for w in jieba.lcut(text) if w.strip()]
    digits = [d for d in _DIGIT_TOKEN_RE.findall(text) if d]
    return words + digits


# 小语料（如 N≤2）下 BM25Okapi 的 idf 会整体塌缩：df=1 时 raw idf 恰为 log(1.5)-log(1.5)=0，
# 负 idf 被 epsilon*average_idf（同样≤0）兜底——命中文档得分全为 0，会被 search 的 <=0
# 阈值全部滤掉。把非正 idf 抬到小正常量以恢复语义：含查询词的文档必得正分，
# 不含查询词的仍为 0 分照旧过滤。大语料下 idf 天然为正，此地板无感。
_MIN_IDF = 0.05


class _FlooredBM25Okapi(BM25Okapi):
    def _calc_idf(self, nd):
        super()._calc_idf(nd)
        for word, value in self.idf.items():
            if value <= 0:
                self.idf[word] = _MIN_IDF


class BM25Index:
    def __init__(self, vector_store):
        self._store = vector_store
        self._lock = threading.Lock()
        self._entries: list[Document] = []
        self._bm25: BM25Okapi | None = None

    # ── 构建 / 增量 ──
    def rebuild_from_store(self) -> int:
        data = self._store.get(include=["documents", "metadatas"]) or {}
        entries = [
            Document(page_content=text, metadata=dict(meta or {}))
            for text, meta in zip(data.get("documents") or [], data.get("metadatas") or [])
            if text
        ]
        with self._lock:
            self._entries = entries
            self._retrain_locked()
        logger.info("BM25Index: 从 store 重建 %d 个分片", len(entries))
        return len(entries)

    def add_chunks(self, chunks: list) -> None:
        if not chunks:
            return
        with self._lock:
            self._entries.extend(chunks)
            self._retrain_locked()

    def remove_source(self, source: str, owner: str | None) -> None:
        with self._lock:
            kept = [d for d in self._entries if not self._match(d, source, owner)]
            if len(kept) != len(self._entries):
                self._entries = kept
                self._retrain_locked()

    @staticmethod
    def _match(doc: Document, source: str, owner: str | None) -> bool:
        meta = doc.metadata or {}
        if meta.get("source") != source:
            return False
        return owner is None or meta.get("user_id") == owner

    def _retrain_locked(self) -> None:
        corpus = [tokenize(d.page_content) for d in self._entries]
        self._bm25 = _FlooredBM25Okapi(corpus) if any(corpus) else None

    # ── 检索 ──
    def search(self, query: str, k: int, viewer_id: str | None) -> list:
        tokens = tokenize(query)
        if not tokens or k <= 0:
            return []
        with self._lock:
            if self._bm25 is None:
                return []
            scores = self._bm25.get_scores(tokens)
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            hits: list[Document] = []
            for i in order:
                if scores[i] <= 0:
                    break
                doc = self._entries[i]
                meta = doc.metadata or {}
                if viewer_id and meta.get("user_id") not in (viewer_id, "system"):
                    continue
                hits.append(doc)
                if len(hits) >= k:
                    break
            return hits
