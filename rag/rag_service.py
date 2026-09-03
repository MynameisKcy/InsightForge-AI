"""
总结服务类：用户提问，搜索参考资料，将参考资料和提问提供给LLM,让模型总结回复
"""
import os
import textwrap
import threading

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from model.factory import get_chat_model
from rag.retrieval_query_rewriter import RetrievalQueryRewriter
from rag.vector_store import VectorStoreService, get_default_vector_store
from utils.config_handler import rag_conf
from utils.logger_handler import logger
from utils.prompt_loader import load_rag_prompts
from utils.tracing import get_tracer, record_exception, traced


def format_context_block(docs: list[Document]) -> str:
    """把检索文档格式化为提示词上下文块——全仓唯一实现。

    生产 rag_summarize 与 ragas 评估（tests/rag_eval）共用此函数：
    评估度量的就是生产 prompt，生产格式一改评估自动锁步（架构评审 R2 候选8）。
    空列表返回空串。
    """
    context = ""
    for counter, doc in enumerate(docs, start=1):
        context += f"[参考资料{counter}] 内容:{doc.page_content} | 元数据:{doc.metadata}\n"
    return context


class RagSummarizerService:
    def __init__(self, vector_store: VectorStoreService | None = None):
        # 注入优先：评估/测试传隔离实例（独立 persist 目录 + 唯一 collection），
        # 不传则取进程级共享单例（与 api.deps 同一实例，见 vector_store.get_default_vector_store）
        self.vector_store = vector_store or get_default_vector_store()
        # 注意：不构造无 owner 过滤的 retriever——检索一律走
        # retriever_docs → _coarse_retrieve → similarity_search(user_id=...)，
        # 按 owner 隔离（自己 + 公共 system）；无过滤 retriever 属泄漏旁路，已删除。
        # rerank 配置（来自 config/rag.yml，复用 .env 的 DASHSCOPE_API_KEY）
        self.rerank_model = rag_conf.get("rerank_model", "gte-rerank")
        # 粗召回数量：向量检索返回的候选池（rerank 前）
        self.retrieve_k = int(rag_conf.get("retrieve_k", 15))
        # rerank 后最终保留的文档数
        self.rerank_top_n = int(rag_conf.get("rerank_top_n", 3))
        # rerank 分数阈值，低于此分丢弃
        self.rerank_score_threshold = float(rag_conf.get("rerank_score_threshold", 0.3))
        self.prompt_text = load_rag_prompts()
        self.prompt_template = PromptTemplate.from_template(self.prompt_text)
        # 默认链（.env 模型）：仅供无 user_id 的旧调用/测试回退；
        # 正式调用（rag_summarize 带 user_id）走 _get_chain 按用户解析模型，
        # 避免单例 service 把所有用户钉死在 .env 默认模型上（网页配置失效 → 403）。
        self.model = get_chat_model()
        self.chain = self.__init_chain()
        # 检索查询扩展器（多查询改写，扩大粗召回召回率）；按 user_id 延迟取模型，
        # 单例 service 下仍能按用户隔离 LLM。失败时 retriever_docs 自动回退单查询。
        self._query_rewriter = RetrievalQueryRewriter()
        # ── Hybrid 双路检索装配（方案一）：BM25 词法路与向量粗召回 RRF 融合。
        # hybrid_enabled=false 时不构建索引、不走融合，dense 行为与历史逐字节一致。
        # 注入隔离 store 的场景（测试/评估）同样生效；store 缺少注册方法时跳过注册。──
        self.hybrid_enabled = bool(rag_conf.get("hybrid_enabled", True))
        self.rrf_k = int(rag_conf.get("rrf_k", 60))
        self.bm25_top_k = int(rag_conf.get("bm25_top_k", 15))
        self._bm25 = None
        if self.hybrid_enabled:
            from rag.bm25 import BM25Index
            index = BM25Index(self.vector_store)
            index.rebuild_from_store()
            for reg_name, callback in (
                ("add_chunks_listener", index.add_chunks),
                ("add_source_deleted_listener", index.remove_source),
                ("add_reindexed_listener", lambda: index.rebuild_from_store()),
            ):
                register = getattr(self.vector_store, reg_name, None)
                if callable(register):
                    register(callback)
            self._bm25 = index

    def __init_chain(self):
        chain = self.prompt_template | self.model |StrOutputParser()
        return chain

    def _get_chain(self, user_id: str | None = None):
        """按 user_id 解析摘要链：用户配置（网页设置）> .env 默认。

        与 RetrievalQueryRewriter 的"单例 service 下按 user_id 延迟取模型"一致：
        带 user_id 时即时构建（factory 内部按 user_id 缓存模型实例，且配置热更新后
        能取到新模型）；无 user_id 回退 __init__ 构建的默认链 self.chain
        （兼容旧调用与测试桩注入的 chain）。
        """
        if not user_id:
            return self.chain
        return self.prompt_template | get_chat_model(user_id) | StrOutputParser()

    def _coarse_retrieve(self, query: str, user_id: str | None = None) -> list[Document]:
        """向量粗召回：用 retrieve_k 拉大候选池，供 rerank 精排。按 owner 过滤（自己+公共 system）。"""
        return self.vector_store.similarity_search(query, user_id=user_id, k=self.retrieve_k)

    def _rerank(self, query: str, docs: list[Document]) -> list[Document]:
        """用 DashScope gte-rerank 精排，取 top_n 并丢弃低分文档；委托 rag.rerank.rerank_docs。

        候选 <= rerank_top_n 时直接截断返回，不触达 rerank_model 等配置（既有契约：
        测试据此在不配置 rerank_model 时断言不调 DashScope）。
        """
        if not docs:
            return []
        if len(docs) <= self.rerank_top_n:
            return docs[:self.rerank_top_n]
        # OTel Span：rerank 失败/回退记录 fallback（算法本体在 rag/rerank.py 共享模块）
        from rag.rerank import rerank_docs
        span = get_tracer().start_span("rag.rerank")
        span.set_attribute("rag.coarse_count", len(docs))
        span.set_attribute("rag.top_n", self.rerank_top_n)
        span.set_attribute("rag.score_threshold", self.rerank_score_threshold)
        try:
            final = rerank_docs(
                query, docs, self.rerank_top_n, self.rerank_model, self.rerank_score_threshold
            )
            span.set_attribute("rag.kept_count", len(final))
            return final
        except Exception as e:
            record_exception(span, e)
            span.set_attribute("rag.fallback", True)
            raise
        finally:
            span.end()

    def _expand_queries(self, query: str, user_id: str | None = None) -> list[str]:
        """多查询扩展：返回原始 query + N 条改写。无改写器/失败时回退 [原始 query]。"""
        rewriter = getattr(self, "_query_rewriter", None)
        if rewriter is None or not query:
            return [query] if query else []
        try:
            return rewriter.expand(query, user_id)
        except Exception as e:
            logger.warning("Query expand failed, using original query only: %s", e)
            return [query] if query else []

    @staticmethod
    def _doc_id(doc: Document) -> str:
        """chunk 去重键：优先 metadata id，否则 source+内容前缀。"""
        mid = (doc.metadata or {}).get("id")
        if mid:
            return f"id:{mid}"
        source = (doc.metadata or {}).get("source", "")
        return f"{source}::{doc.page_content[:80]}"

    def _aggregate_bm25(self, queries: list[str], user_id: str | None = None) -> list[Document]:
        """BM25 路：每个扩展 query 各查一次，同一 doc 取最好名次，重排后截断 top bm25_top_k。
        名次（而非原始分数）做跨 query 比较，规避分数不可加的问题。
        最好名次并列时后取得该名次者优先——仅作确定性并列裁决（结果稳定可复现），
        不承载"共识文档靠前"语义；下游 RRF 融合与 rerank 精排不依赖此并列微序。
        单 query 失败降级为空列表，不拖垮整条 BM25 路。"""
        best: dict[str, tuple[int, int]] = {}  # doc_id -> (best_rank, 取得该名次时的序号)
        docs: dict[str, Document] = {}
        seq = 0
        for q in queries:
            try:
                hits = self._bm25.search(q, k=self.bm25_top_k, viewer_id=user_id)
            except Exception as e:
                logger.warning("BM25 leg failed (degrade to empty): %s", e)
                hits = []
            for rank, doc in enumerate(hits):
                did = self._doc_id(doc)
                cur = best.get(did)
                if cur is None or rank < cur[0]:
                    best[did] = (rank, seq)
                docs[did] = doc
                seq += 1
        ordered_ids = sorted(best, key=lambda d: (best[d][0], -best[d][1]))
        return [docs[d] for d in ordered_ids][: self.bm25_top_k]

    @staticmethod
    def _rrf_fuse(list_a: list[Document], list_b: list[Document], k: int) -> list[Document]:
        """标准 RRF：score(d)=Σ_lists 1/(k+rank)。并列时按首次出现序稳定排序。"""
        scores: dict[str, float] = {}
        first_seq: dict[str, int] = {}
        pool: dict[str, Document] = {}
        seq = 0
        for ranked in (list_a, list_b):
            for rank, doc in enumerate(ranked):
                did = RagSummarizerService._doc_id(doc)
                scores[did] = scores.get(did, 0.0) + 1.0 / (k + rank)
                pool.setdefault(did, doc)
                first_seq.setdefault(did, seq)
                seq += 1
        ordered = sorted(scores, key=lambda d: (-scores[d], first_seq[d]))
        return [pool[d] for d in ordered]

    def _coarse_pool(self, query: str, user_id: str | None = None) -> list[Document]:
        """rerank 前候选池：多查询扩展 → dense 粗召回(原逻辑不动) (+ BM25 路 RRF 融合)。
        hybrid 关闭或 BM25 异常时退化为纯 dense 列表。"""
        queries = self._expand_queries(query, user_id)
        seen_ids: set[str] = set()
        dense: list[Document] = []
        for q in queries:
            for doc in self._coarse_retrieve(q, user_id):
                did = self._doc_id(doc)
                if did in seen_ids:
                    continue
                seen_ids.add(did)
                dense.append(doc)
        if self._bm25 is None:
            return dense
        bm = self._aggregate_bm25(queries, user_id)
        return self._rrf_fuse(dense, bm, self.rrf_k)

    def retriever_docs(self, query: str, user_id: str | None = None) -> list[Document]:
        # 多查询扩展 + 双路粗召回(dense+BM25 经 RRF 融合) + rerank 精排
        # （ADR-0002 查询改写 / docs/specs/2026-08-26-rag-scheme1-hybrid-retrieval-design.md）
        with traced("rag.retrieve", attrs={
                "rag.query": query[:200],
                "rag.k": self.retrieve_k,
                "rag.user_id": user_id or "",
                "rag.bm25_enabled": self._bm25 is not None,
        }) as span:
            pool = self._coarse_pool(query, user_id)
            span.set_attribute("rag.coarse_count", len(pool))
            final = self._rerank(query, pool)
            span.set_attribute("rag.results_count", len(final))
            return final

    @staticmethod
    def _format_doc_source(doc: Document, index: int) -> str:
        metadata = doc.metadata or {}
        source = metadata.get("source") or metadata.get("file_path") or "未知来源"
        source_name = os.path.basename(str(source))

        page = metadata.get("page")
        page_text = ""
        if page is not None:
            try:
                page_text = f"，第{int(page) + 1}页"
            except (TypeError, ValueError):
                page_text = f"，页码:{page}"

        excerpt = " ".join(str(doc.page_content).split())
        excerpt = textwrap.shorten(excerpt, width=120, placeholder="...")
        return f"{index}. {source_name}{page_text}：{excerpt}"

    def format_reference_sources(self, docs: list[Document]) -> str:
        if not docs:
            return "## 参考来源\n未检索到可引用的知识库资料。"

        lines = ["## 参考来源"]
        for index, doc in enumerate(docs, start=1):
            lines.append(self._format_doc_source(doc, index))
        return "\n".join(lines)

    def rag_summarize(self, query: str, user_id: str | None = None) -> str:
        context_docs = self.retriever_docs(query, user_id)
        context = format_context_block(context_docs)

        answer = self._get_chain(user_id).invoke(
            {
                "input": query,
                "context": context,
            }
        )
        references = self.format_reference_sources(context_docs)
        return f"{answer.strip()}\n\n{references}"


# 进程级摘要服务单例（先例：memory/recall.py:get_memory_recall）。
# agent_tools 懒获取用——避免每次工具调用重建 service 连带重建 BM25 索引。
# 双重检查锁定（与 vector_store.get_default_vector_store 同款）：Hybrid 装配后
# __init__ 会向共享 store 注册写回调——构造竞态会让回调重复注册且永不摘除。
_default_service = None
_lock = threading.Lock()


def get_default_rag_summarizer():
    global _default_service
    if _default_service is None:
        with _lock:
            if _default_service is None:
                _default_service = RagSummarizerService()
    return _default_service


def reset_default_rag_summarizer() -> None:
    """测试专用。"""
    global _default_service
    with _lock:
        _default_service = None


if __name__ == "__main__":
    rag = RagSummarizerService()

    res = rag.rag_summarize("小机器人使用需要注意什么")
    print(res)
