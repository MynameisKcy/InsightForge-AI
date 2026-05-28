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
    from agent.utils.prompt_loader import load_rag_prompts
    from agent.model.factory import chat_model
except ModuleNotFoundError:
    from rag.vector_store import VectorStoreService
    from utils.prompt_loader import load_rag_prompts
    from model.factory import chat_model


class RagSummarizerService(object):
    def __init__(self):
        self.vector_store = VectorStoreService()
        self.retriever = self.vector_store.get_retriver()
        self.prompt_text = load_rag_prompts()
        self.prompt_template = PromptTemplate.from_template(self.prompt_text)
        self.model = chat_model
        self.chain = self.__init_chain()

    def __init_chain(self):
        chain = self.prompt_template | self.model |StrOutputParser()
        return chain

    def retriever_docs(self,query:str) -> list[Document]:
        if hasattr(self.retriever, "invoke"):
            return self.retriever.invoke(query)
        if hasattr(self.retriever, "get_relevant_documents"):
            return self.retriever.get_relevant_documents(query)
        if hasattr(self.retriever, "retrieve"):
            return self.retriever.retrieve(query)
        raise AttributeError("retriever does not support invoke/get_relevant_documents/retrieve")

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

    def rag_summarize(self,query:str) -> str:
        context_docs = self.retriever_docs(query)

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
