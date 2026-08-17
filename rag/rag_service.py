"""
总结服务类：用户提问，搜索参考资料，将参考资料和提问提供给LLM,让模型总结回复
"""
import os
import textwrap

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser

from langchain_core.prompts import PromptTemplate

from rag.vector_store import VectorStoreService
from rag.retrieval_query_rewriter import RetrievalQueryRewriter
from utils.prompt_loader import load_rag_prompts
from utils.config_handler import rag_conf
from model.factory import get_chat_model

from utils.logger_handler import logger


class RagSummarizerService(object):
    def __init__(self):
        self.vector_store = VectorStoreService()
        self.retriever = self.vector_store.get_retriver()
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
        from rag.rerank import rerank_docs
        return rerank_docs(
            query, docs, self.rerank_top_n, self.rerank_model, self.rerank_score_threshold
        )

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

    def retriever_docs(self, query: str, user_id: str | None = None) -> list[Document]:
        # 多查询扩展 + 粗召回大候选池 + rerank 精排，提升中文检索召回率与相关性
        # （详见 docs/adr/0002-query-rewriting-two-points.md）
        queries = self._expand_queries(query, user_id)
        seen_ids: set[str] = set()
        coarse: list[Document] = []
        for q in queries:
            for doc in self._coarse_retrieve(q, user_id):
                did = self._doc_id(doc)
                if did in seen_ids:
                    continue
                seen_ids.add(did)
                coarse.append(doc)
        # 精排仍用原始 query 打分（改写只为扩大召回，不参与排序）
        return self._rerank(query, coarse)

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

        context = ""
        counter = 0
        for doc in context_docs:
            counter += 1
            context += f"[参考资料{counter}] 内容:{doc.page_content} | 元数据:{doc.metadata}\n"

        answer = self._get_chain(user_id).invoke(
            {
                "input": query,
                "context": context,
            }
        )
        references = self.format_reference_sources(context_docs)
        return f"{answer.strip()}\n\n{references}"

if __name__ == "__main__":
    rag = RagSummarizerService()

    res = rag.rag_summarize("小机器人使用需要注意什么")
    print(res)
