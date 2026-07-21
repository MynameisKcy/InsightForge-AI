import json
import os.path
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter

try:
    from agent.utils.file_handler import listdir_with_allowed_type, get_file_md5_hex
    from agent.utils.file_handler import text_loader, pdf_loader, docx_loader, markdown_loader
    from agent.utils.logger_handler import logger
    from agent.utils.path_tool import get_abs_path
    from agent.utils.config_handler import chroma_conf
    from agent.model.factory import get_embed_model
except ModuleNotFoundError:
    from utils.file_handler import listdir_with_allowed_type, get_file_md5_hex
    from utils.file_handler import text_loader, pdf_loader, docx_loader, markdown_loader
    from utils.logger_handler import logger
    from utils.path_tool import get_abs_path
    from utils.config_handler import chroma_conf
    from model.factory import get_embed_model


def _load_file_documents(read_path: str) -> list[Document]:
    """按扩展名分发到对应的加载器，统一返回 Document 列表。"""
    lower = read_path.lower()
    if lower.endswith(".txt"):
        return text_loader(read_path)
    if lower.endswith(".pdf"):
        return pdf_loader(read_path)
    if lower.endswith(".docx"):
        return docx_loader(read_path)
    if lower.endswith(".md"):
        return markdown_loader(read_path)
    return []


class VectorStoreService:
    def __init__(self, config_path="config/rag.yml"):
        self.config_path = config_path
        self.vector_store = Chroma(
            collection_name=chroma_conf["collection_name"],
            embedding_function=get_embed_model(),
            persist_directory=get_abs_path(chroma_conf["persist_directory"]),
        )
        self.spliter = RecursiveCharacterTextSplitter(
            chunk_size=chroma_conf["chunk_size"],
            chunk_overlap=chroma_conf["chunk_overlap"],
            separators=chroma_conf["separators"],
            length_function=len,
        )

    # ── md5 存储：json set，O(1) 查询 + 支持按文件删除 ──
    def _md5_store_path(self) -> str:
        return get_abs_path(chroma_conf["md5_hex_store"])

    def _load_md5_store(self) -> set:
        """加载 md5 集合；兼容旧版逐行 md5.text（迁移为 set）。"""
        path = self._md5_store_path()
        if not os.path.exists(path):
            return set()
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                return set()
            # 新格式：json 数组
            if content.startswith("["):
                return set(json.loads(content))
            # 旧格式：逐行 md5
            legacy = {line.strip() for line in content.splitlines() if line.strip()}
            # 顺手迁移为 json
            self._save_md5_store(legacy)
            return legacy
        except Exception as e:
            logger.error(f"读取 md5 存储失败: {e}")
            return set()

    def _save_md5_store(self, store: set) -> None:
        path = self._md5_store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(store), f, ensure_ascii=False)

    def _check_md5(self, md5_for_check: str) -> bool:
        return md5_for_check in self._load_md5_store()

    def _add_md5(self, md5_for_check: str) -> None:
        store = self._load_md5_store()
        if md5_for_check not in store:
            store.add(md5_for_check)
            self._save_md5_store(store)

    def _remove_md5(self, md5_for_check: str) -> None:
        store = self._load_md5_store()
        if md5_for_check in store:
            store.discard(md5_for_check)
            self._save_md5_store(store)

    def get_retriver(self):
        return self.vector_store.as_retriever(search_kwargs={"k": chroma_conf["k"]})

    def load_document(self):
        """批量加载 data/ 下所有允许类型的知识库文件（增量，已入库的跳过）。"""
        loaded_count = 0
        available_count = 0

        allowed_files_path = listdir_with_allowed_type(
            get_abs_path(chroma_conf["data_path"]),
            tuple(chroma_conf["allowed_knowledge_file_type"]),
        )

        if not allowed_files_path:
            logger.warning("未找到可加载的知识库文件")
            return loaded_count, available_count

        for path in allowed_files_path:
            md5_hex = get_file_md5_hex(path)
            if not md5_hex:
                logger.warning(f"跳过无效文件:{path}")
                continue
            if self._check_md5(md5_hex):
                logger.info(f"加载知识库:{path}已存在知识库")
                available_count += 1
                continue
            n = self._ingest_file(path, md5_hex)
            if n > 0:
                loaded_count += 1
                available_count += 1

        return loaded_count, available_count

    def _ingest_file(self, path: str, md5_hex: str | None = None) -> int:
        """加载单个文件入库，返回分片数。失败返回 0。"""
        if md5_hex is None:
            md5_hex = get_file_md5_hex(path)
        if not md5_hex:
            return 0
        try:
            documents: list[Document] = _load_file_documents(path)
            if not documents:
                logger.warning(f"加载知识库：{path}内无有效内容")
                return 0
            split_document = self.spliter.split_documents(documents)
            if not split_document:
                logger.warning(f"{path}分片后无有效内容")
                return 0
            # 统一写入 source 元数据，便于按来源删除
            for doc in split_document:
                doc.metadata.setdefault("source", path)
                doc.metadata.setdefault("file_md5", md5_hex)
            self.vector_store.add_documents(split_document)
            self._add_md5(md5_hex)
            logger.info(f"{path}加载成功，{len(split_document)} 个分片")
            return len(split_document)
        except Exception as e:
            logger.error(f"{path}加载失败:{str(e)}", exc_info=True)
            return 0

    def load_single_document(self, path: str):
        """运行时增量入库单个文件（知识库管理接口用）。
        返回 (loaded_chunks, skipped) — skipped=True 表示已存在跳过。
        """
        if not os.path.exists(path):
            logger.error(f"文件不存在: {path}")
            return 0, False
        md5_hex = get_file_md5_hex(path)
        if not md5_hex:
            return 0, False
        if self._check_md5(md5_hex):
            logger.info(f"运行时入库：{path} 已存在，跳过")
            return 0, True
        n = self._ingest_file(path, md5_hex)
        return n, False

    def delete_by_source(self, source: str) -> int:
        """按来源文件路径删除所有相关分片，并移除 md5 记录。返回删除的估计数量。"""
        try:
            # 先取该 source 的 chunk 数用于返回
            try:
                existing = self.vector_store.get(where={"source": source})
                count = len(existing.get("ids", []) or [])
            except Exception:
                count = 0
            self.vector_store.delete(where={"source": source})
            # 移除 md5（按文件重算）
            if os.path.exists(source):
                md5_hex = get_file_md5_hex(source)
                if md5_hex:
                    self._remove_md5(md5_hex)
            logger.info(f"已删除来源 {source} 的 {count} 个分片")
            return count
        except Exception as e:
            logger.error(f"删除来源 {source} 失败: {e}", exc_info=True)
            return 0

    def get_stats(self) -> dict:
        """返回知识库统计：总 chunk 数、唯一来源数、嵌入维度、collection 名。"""
        try:
            data = self.vector_store.get()
            ids = data.get("ids", []) or []
            metadatas = data.get("metadatas", []) or []
            sources = {m.get("source") for m in metadatas if m and m.get("source")}
            # 嵌入维度
            embeddings = data.get("embeddings")
            dim = len(embeddings[0]) if embeddings else 0
            return {
                "total_chunks": len(ids),
                "total_sources": len(sources),
                "embedding_dim": dim,
                "collection_name": chroma_conf["collection_name"],
                "persist_directory": chroma_conf["persist_directory"],
            }
        except Exception as e:
            logger.error(f"获取统计失败: {e}")
            return {"total_chunks": 0, "total_sources": 0, "embedding_dim": 0,
                    "collection_name": chroma_conf["collection_name"],
                    "persist_directory": chroma_conf["persist_directory"]}

    def reindex_all(self) -> dict:
        """清空向量库与 md5 记录，全量重新入库 data/ 下所有文件。"""
        try:
            # 清空 collection
            data = self.vector_store.get()
            ids = data.get("ids", []) or []
            if ids:
                self.vector_store.delete(ids=ids)
            logger.info(f"reindex: 已清空 {len(ids)} 个旧分片")
        except Exception as e:
            logger.error(f"reindex 清空失败: {e}")
        # 清空 md5
        self._save_md5_store(set())
        # 全量重灌
        loaded, available = self.load_document()
        return {"reloaded_files": loaded, "total_files": available,
                "stats": self.get_stats()}


if __name__ == "__main__":
    vs = VectorStoreService()
    loaded_count, available_count = vs.load_document()
    if available_count > 0:
        print("加载成功")
    else:
        print("加载失败")
