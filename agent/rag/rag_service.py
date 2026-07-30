"""
总结服务类：用户提问，搜索参考资料，将参考资料和提问提供给LLM,让模型总结回复
"""
import os
import sys
import textwrap

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
if PROJECT_PARENT not in sys.path:
    sys.path.insert(0, PROJECT_PARENT)

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser

from langchain_core.prompts import PromptTemplate

try:
    from agent.rag.vector_store import VectorStoreService
    from agent.rag.retrieval_query_rewriter import RetrievalQueryRewriter
    from agent.utils.prompt_loader import load_rag_prompts
    from agent.utils.config_handler import rag_conf
    from agent.model.factory import get_chat_model
except ModuleNotFoundError:
    from rag.vector_store import VectorStoreService
    from rag.retrieval_query_rewriter import RetrievalQueryRewriter
    from utils.prompt_loader import load_rag_prompts
    from utils.config_handler import rag_conf
    from model.factory import get_chat_model

try:
    from utils.logger_handler import logger
except ModuleNotFoundError:
    from agent.utils.logger_handler import logger


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
        self.model = get_chat_model()
        self.chain = self.__init_chain()
        # 检索查询扩展器（多查询改写，扩大粗召回召回率）；按 user_id 延迟取模型，
        # 单例 service 下仍能按用户隔离 LLM。失败时 retriever_docs 自动回退单查询。
        self._query_rewriter = RetrievalQueryRewriter()

    def __init_chain(self):
        chain = self.prompt_template | self.model |StrOutputParser()
        return chain

    def _coarse_retrieve(self, query: str, user_id: str | None = None) -> list[Document]:
        """向量粗召回：用 retrieve_k 拉大候选池，供 rerank 精排。按 owner 过滤（自己+公共 system）。"""
        return self.vector_store.similarity_search(query, user_id=user_id, k=self.retrieve_k)

    def _rerank(self, query: str, docs: list[Document]) -> list[Document]:
        """用 DashScope gte-rerank 对粗召回结果精排，取 top_n 并丢弃低分文档。

        降级策略：rerank 调用失败时回退为粗召回结果的前 top_n 条，保证可用性。
        """
        if not docs:
            return []
        # 候选数已不多于 top_n 时无需 rerank
        if len(docs) <= self.rerank_top_n:
            return docs[: self.rerank_top_n]
        try:
            from dashscope import TextReRank
            import os as _os
            resp = TextReRank.call(
                model=self.rerank_model,
                query=query,
                documents=[d.page_content for d in docs],
                top_n=self.rerank_top_n,
                return_documents=False,
                api_key=_os.getenv("DASHSCOPE_API_KEY"),
            )
            # 健壮性：先判 HTTP 状态与 output 是否为空（403/AccessDenied 时 output=None）
            status = getattr(resp, "status_code", None)
            output = getattr(resp, "output", None)
            if status != 200 or not output or not output.get("results"):
                code = getattr(resp, "code", "")
                message = getattr(resp, "message", "")
                logger.warning(
                    "rerank 调用未返回有效结果 (status=%s code=%s msg=%s)，回退粗召回前 %d 条",
                    status, code, message, self.rerank_top_n,
                )
                return docs[: self.rerank_top_n]
            results = output.get("results", [])
            reranked: list[Document] = []
            for item in results:
                idx = item.get("index")
                score = item.get("relevance_score", 0)
                if idx is None or idx < 0 or idx >= len(docs):
                    continue
                if score < self.rerank_score_threshold:
                    continue
                doc = docs[idx]
                # 把 rerank 分数写入 metadata，便于调试展示
                doc.metadata["rerank_score"] = score
                reranked.append(doc)
            logger.info(
                "rerank: 粗召回 %d 条 -> 精排 %d 条 (阈值 %.2f)",
                len(docs), len(reranked), self.rerank_score_threshold,
            )
            return reranked if reranked else docs[: self.rerank_top_n]
        except Exception as e:
            logger.error("rerank 调用失败，回退粗召回: %s", str(e))
            return docs[: self.rerank_top_n]

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

        answer = self.chain.invoke(
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
