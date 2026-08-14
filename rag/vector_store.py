import json
import os.path
import threading

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter

from utils.file_handler import listdir_with_allowed_type, get_file_md5_hex
from utils.file_handler import text_loader, pdf_loader, docx_loader, markdown_loader
from utils.logger_handler import logger
from utils.path_tool import get_abs_path
from utils.config_handler import chroma_conf
from model.factory import get_embed_model


# 公共 / 历史 owner：迁移前无 user_id 的分片统一归属 system，作为对所有用户可见的公共知识。
PUBLIC_OWNER = "system"

# 跨会话记忆召回专用 collection（ADR-0003 Phase 3）：存会话终版摘要，与知识库 collection 分离
MEMORY_COLLECTION = "memory"


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
    def __init__(self, config_path="config/rag.yml", collection_name: str | None = None):
        self.config_path = config_path
        # collection_name=None 用默认知识库 collection；传 MEMORY_COLLECTION 走跨会话记忆召回
        self.collection_name = collection_name or chroma_conf["collection_name"]
        self.vector_store = Chroma(
            collection_name=self.collection_name,
            embedding_function=get_embed_model(),
            persist_directory=get_abs_path(chroma_conf["persist_directory"]),
        )
        self.spliter = RecursiveCharacterTextSplitter(
            chunk_size=chroma_conf["chunk_size"],
            chunk_overlap=chroma_conf["chunk_overlap"],
            separators=chroma_conf["separators"],
            length_function=len,
        )
        # 仅默认知识库 collection 做历史 owner 迁移；memory collection 无历史分片，跳过
        if self.collection_name == chroma_conf["collection_name"]:
            self._migrate_legacy_owner()
        # memory collection 并发访问锁：闲置 finalize（后台线程写）与 recall（请求线程读）
        # 同一 memory store 单例，串行化避免 chroma/sqlite 并发竞态（ADR-0003 Phase 4 修订）
        self._lock = threading.Lock()

    # ── owner 过滤器 ──
    @staticmethod
    def _owner_filter(user_id: str | None, include_public: bool = True) -> dict:
        """构造 Chroma where 过滤器。
        include_public=True（检索用）：自己 + 公共 system；
        include_public=False（删除/列表/统计用）：仅自己。
        user_id 为空时返回 {}（不过滤，兼容全量场景）。"""
        if not user_id:
            return {}
        if include_public:
            return {"$or": [{"user_id": user_id}, {"user_id": PUBLIC_OWNER}]}
        return {"user_id": user_id}

    @staticmethod
    def _where_source_owner(source: str, user_id: str | None) -> dict:
        """按 source(+owner) 构造 Chroma where 子句。

        多条件必须用 $and 显式组合：chromadb 的 delete(where=...) 只接受单一操作符，
        直接写 {"source":..,"user_id":..} 会被拒（get 接受、delete 不接受），
        统一用 $and 规避版本差异。user_id 为空时仅按 source 过滤。
        """
        if user_id:
            return {"$and": [{"source": source}, {"user_id": user_id}]}
        return {"source": source}

    def _migrate_legacy_owner(self) -> None:
        """把缺少 user_id 元数据的历史分片标记为 PUBLIC_OWNER（system），作为公共知识可见。
        幂等：已有 user_id 的不动。失败不影响主流程。"""
        try:
            col = getattr(self.vector_store, "_collection", None) or getattr(self.vector_store, "collection", None)
            if col is None:
                return
            data = col.get(include=["metadatas"])
            ids = data.get("ids") or []
            metas = data.get("metadatas") or []
            upd_ids, upd_metas = [], []
            for _id, m in zip(ids, metas):
                if not m or "user_id" not in m:
                    upd_ids.append(_id)
                    upd_metas.append({"user_id": PUBLIC_OWNER})
            if upd_ids:
                col.update(ids=upd_ids, metadatas=upd_metas)
                logger.info(f"owner 迁移：{len(upd_ids)} 个历史分片标记为 {PUBLIC_OWNER}")
        except Exception as e:
            logger.warning(f"历史 owner 迁移失败（可忽略）: {e}")

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

    # ── chroma 实际状态查询：md5 仅作去重提示，chroma 才是“可读”的真相 ──
    # 修复 md5 存储与 chroma 偏离（md5 在、chroma 空）导致“显示已入库但读不到”的 bug。
    def chroma_sources(self, user_id: str | None = None) -> set:
        """返回 Chroma 中实际存在分片的 source 路径集合（一次 get 调用）。
        user_id 给定时仅返回该用户的来源；None 时全量（兼容）。"""
        try:
            where = self._owner_filter(user_id, include_public=False)
            metas = self.vector_store.get(where=where).get("metadatas") or []
            return {m.get("source") for m in metas if m and m.get("source")}
        except Exception as e:
            logger.error(f"读取 chroma sources 失败: {e}")
            return set()

    def _source_has_chunks(self, source: str, user_id: str | None = None) -> bool:
        """chroma 中是否存在该来源的分片。user_id 给定时限定 owner。"""
        try:
            data = self.vector_store.get(where=self._where_source_owner(source, user_id))
            return bool(data and data.get("ids"))
        except Exception:
            return False

    def get_retriver(self, user_id: str | None = None):
        sk = {"k": chroma_conf["k"]}
        flt = self._owner_filter(user_id, include_public=True)
        if flt:
            sk["filter"] = flt
        return self.vector_store.as_retriever(search_kwargs=sk)

    def similarity_search(self, query: str, user_id: str | None = None, k: int | None = None):
        """向量检索：按 owner 过滤（自己 + 公共 system）。user_id 为空时全量。"""
        try:
            kk = k or int(chroma_conf.get("k", 5))
            kwargs = {"k": kk}
            flt = self._owner_filter(user_id, include_public=True)
            if flt:
                kwargs["filter"] = flt
            return self.vector_store.similarity_search(query, **kwargs)
        except Exception as e:
            logger.error(f"向量检索失败: {e}")
            return []

    def _data_dir_for(self, user_id: str | None) -> str:
        """用户知识库落盘目录：data/<user_id>/。"""
        base = get_abs_path(chroma_conf["data_path"])
        uid = user_id or "default"
        return os.path.join(base, uid)

    def load_document(self, user_id: str | None = None):
        """批量加载知识库文件。user_id 给定时只加载 data/<user_id>/；
        None 时遍历 data/ 下所有用户子目录（兼容批量 / __main__）。"""
        loaded_count = 0
        available_count = 0
        base = get_abs_path(chroma_conf["data_path"])
        if user_id:
            targets = [(user_id, self._data_dir_for(user_id))]
        else:
            targets = []
            if os.path.isdir(base):
                for name in sorted(os.listdir(base)):
                    sub = os.path.join(base, name)
                    if os.path.isdir(sub):
                        targets.append((name, sub))
        if not targets:
            logger.warning("未找到可加载的知识库文件目录")
            return loaded_count, available_count
        allowed_types = tuple(chroma_conf["allowed_knowledge_file_type"])
        for uid, d in targets:
            allowed_files_path = listdir_with_allowed_type(d, allowed_types)
            if not allowed_files_path:
                continue
            for path in allowed_files_path:
                md5_hex = get_file_md5_hex(path)
                if not md5_hex:
                    logger.warning(f"跳过无效文件:{path}")
                    continue
                n, skipped = self._ingest_if_needed(path, md5_hex, uid)
                if n > 0:
                    loaded_count += 1
                    available_count += 1
                elif skipped:
                    available_count += 1
        return loaded_count, available_count

    def _ingest_file(self, path: str, md5_hex: str | None = None, user_id: str | None = None) -> int:
        """加载单个文件入库，返回分片数。失败返回 0。分片写入 user_id owner 元数据。"""
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
            uid = user_id or "default"
            # 统一写入 source / file_md5 / user_id 元数据，便于按来源与 owner 删除/过滤
            for doc in split_document:
                doc.metadata.setdefault("source", path)
                doc.metadata.setdefault("file_md5", md5_hex)
                doc.metadata["user_id"] = uid   # owner 隔离关键字段
            self.vector_store.add_documents(split_document)
            self._add_md5(md5_hex)
            logger.info(f"{path}加载成功，{len(split_document)} 个分片 (owner={uid})")
            return len(split_document)
        except Exception as e:
            logger.error(f"{path}加载失败:{str(e)}", exc_info=True)
            return 0

    def _ingest_if_needed(self, path: str, md5_hex: str | None = None, user_id: str | None = None):
        """按需入库：md5 命中 且 chroma 确有该来源分片 才跳过；否则重新入库。

        修复 md5 存储与 chroma 实际状态偏离的 bug（md5 在、chroma 空）：
        偏离时不再无脑 skip，而是清掉该来源残留分片后重灌，保证 已入库=可读。
        内容变更（同文件名不同内容，md5 变）同样走清残+重灌，避免重复分片。
        返回 (loaded_chunks, skipped) - skipped=True 表示已存在且可读、跳过。
        """
        if md5_hex is None:
            md5_hex = get_file_md5_hex(path)
        if not md5_hex:
            return 0, False
        uid = user_id or "default"
        # 真正已入库（md5 命中 且 chroma 确有分片）才跳过
        if self._check_md5(md5_hex) and self._source_has_chunks(path, uid):
            logger.info(f"运行时入库：{path} 已存在且可读，跳过")
            return 0, True
        # md5 在但 chroma 缺失（偏离）/ 内容变更：先清该来源残留分片，避免重复
        if self._source_has_chunks(path, uid):
            try:
                self.vector_store.delete(where=self._where_source_owner(path, uid))
            except Exception as e:
                logger.warning(f"清理旧分片失败 {path}: {e}")
        n = self._ingest_file(path, md5_hex, uid)
        # 入库失败但 md5 残留（历史脏数据）：清掉误导性的 md5，避免假已入库
        if n == 0 and self._check_md5(md5_hex) and not self._source_has_chunks(path, uid):
            self._remove_md5(md5_hex)
        return n, False

    def load_single_document(self, path: str, user_id: str | None = None):
        """运行时增量入库单个文件（知识库管理接口用）。
        返回 (loaded_chunks, skipped) - skipped=True 表示已存在且可读、跳过。
        md5 与 chroma 偏离时会自愈重灌。"""
        if not os.path.exists(path):
            logger.error(f"文件不存在: {path}")
            return 0, False
        return self._ingest_if_needed(path, user_id=user_id)

    def delete_by_source(self, source: str, user_id: str | None = None) -> int:
        """按来源文件路径删除所有相关分片，并移除 md5 记录。返回删除的估计数量。
        user_id 给定时仅删该 owner 的分片（不误删他人同名来源）。"""
        uid = user_id or "default"
        where = self._where_source_owner(source, user_id)
        try:
            # 先取该 source 的 chunk 数用于返回
            try:
                existing = self.vector_store.get(where=where)
                count = len(existing.get("ids", []) or [])
            except Exception:
                count = 0
            self.vector_store.delete(where=where)
            # 移除 md5（按文件重算）
            if os.path.exists(source):
                md5_hex = get_file_md5_hex(source)
                if md5_hex:
                    self._remove_md5(md5_hex)
            logger.info(f"已删除来源 {source} 的 {count} 个分片 (owner={uid})")
            return count
        except Exception as e:
            logger.error(f"删除来源 {source} 失败: {e}", exc_info=True)
            return 0

    def get_stats(self, user_id: str | None = None) -> dict:
        """返回知识库统计：总 chunk 数、唯一来源数、嵌入维度、collection 名。
        user_id 给定时仅统计该用户；None 时全量。"""
        try:
            where = self._owner_filter(user_id, include_public=False)
            data = self.vector_store.get(where=where)
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
                "collection_name": self.collection_name,
                "persist_directory": chroma_conf["persist_directory"],
            }
        except Exception as e:
            logger.error(f"获取统计失败: {e}")
            return {"total_chunks": 0, "total_sources": 0, "embedding_dim": 0,
                    "collection_name": self.collection_name,
                    "persist_directory": chroma_conf["persist_directory"]}

    def reindex_all(self, user_id: str | None = None) -> dict:
        """清空并全量重新入库。user_id 给定时仅清空并重灌该用户目录；
        None 时清空全部（兼容旧的全量行为）。"""
        try:
            if user_id:
                data = self.vector_store.get(where={"user_id": user_id})
                ids = data.get("ids", []) or []
                if ids:
                    self.vector_store.delete(ids=ids)
                logger.info(f"reindex: 已清空 owner={user_id} 的 {len(ids)} 个分片")
            else:
                # 清空 collection
                data = self.vector_store.get()
                ids = data.get("ids", []) or []
                if ids:
                    self.vector_store.delete(ids=ids)
                logger.info(f"reindex: 已清空 {len(ids)} 个旧分片")
        except Exception as e:
            logger.error(f"reindex 清空失败: {e}")
        # 清空 md5（全量重建场景；per-user 重建时 md5 自愈，清空不影响正确性）
        self._save_md5_store(set())
        # 全量重灌
        loaded, available = self.load_document(user_id=user_id)
        return {"reloaded_files": loaded, "total_files": available,
                "stats": self.get_stats(user_id=user_id)}

    # ── 跨会话记忆召回（ADR-0003 Phase 3）：操作 memory collection ──
    # 与知识库分片不同，记忆 Document = 一条会话终版摘要，按 session_id 唯一，user_id owner 隔离。

    def add_session_memory(self, user_id: str, session_id: str, summary: str,
                           title: str = "", ended_at: str = "") -> None:
        """写入/更新一条会话终版摘要到 memory collection（按 session_id 原子 upsert）。

        用 chromadb Collection.upsert(ids=[mem:{session_id}]) 单次 insert-or-replace，
        无 delete-then-add 空窗：旧 delete 成功而 add 失败导致摘要静默丢失的风险消除。
        加锁串行化与 recall 读的并发（同一 memory store 单例）。
        """
        if not summary or not summary.strip():
            return
        col = getattr(self.vector_store, "_collection", None) or getattr(self.vector_store, "collection", None)
        if col is None:
            logger.warning("add_session_memory: 无底层 collection")
            return
        meta = {
            "user_id": user_id,
            "session_id": session_id,
            "title": title or "",
            "ended_at": ended_at or "",
            "source": f"memory:{session_id}",
        }
        # 显式预计算 embedding 并连同 documents 一起 upsert：
        # 直接 col.upsert(documents=...) 会触发 chromadb 自带 embedding_function
        # （默认 ONNX 远程下载，或与 langchain 注入的不一致），在离线/测试环境 SSL 超时。
        # 用 langchain 注入的 embedding_function 先 embed，再传 embeddings= 跳过 chromadb 内部 embed，
        # documents= 仅作原文存储（保留 page_content 供召回展示）。
        embed_fn = getattr(self.vector_store, "embedding_function", None) or getattr(
            self.vector_store, "_embedding_function", None
        )
        with self._lock:
            try:
                if embed_fn is not None:
                    emb = embed_fn.embed_documents([summary])
                    col.upsert(
                        ids=[f"mem:{session_id}"],
                        embeddings=emb,
                        documents=[summary],
                        metadatas=[meta],
                    )
                else:
                    col.upsert(
                        ids=[f"mem:{session_id}"],
                        documents=[summary],
                        metadatas=[meta],
                    )
                logger.info(f"Memory recall: upserted summary for session {session_id} (owner={user_id})")
            except Exception as e:
                logger.warning(f"add_session_memory failed: {e}")

    def retrieve_session_memories(self, query: str, user_id: str | None = None,
                                  k: int = 5, exclude_session_id: str | None = None):
        """召回该用户的历史会话终版摘要（owner 过滤：仅自己，不含公共 system）。

        exclude_session_id 给定时排除当前会话（避免召回自己刚 finalize 的摘要）。
        user_id 为空时全量（兼容场景）。加锁与 finalize 写串行化。
        """
        try:
            flt = self._owner_filter(user_id, include_public=False)  # 仅自己
            if exclude_session_id:
                if flt:
                    flt = {"$and": [flt, {"session_id": {"$ne": exclude_session_id}}]}
                else:
                    flt = {"session_id": {"$ne": exclude_session_id}}
            kwargs = {"k": k}
            if flt:
                kwargs["filter"] = flt
            with self._lock:
                return self.vector_store.similarity_search(query, **kwargs)
        except Exception as e:
            logger.warning(f"retrieve_session_memories failed: {e}")
            return []

    def delete_session_memory(self, session_id: str, user_id: str | None = None) -> None:
        """按 session_id(+owner) 删除其终版摘要 embedding（删会话 / upsert 清旧时调用）。

        多条件用 $and 显式组合：chromadb delete(where=...) 只接受单一操作符，
        直接写 {"session_id":..,"user_id":..} 会被拒（与 _where_source_owner 同理）。加锁串行化。
        """
        if user_id:
            where = {"$and": [{"session_id": session_id}, {"user_id": user_id}]}
        else:
            where = {"session_id": session_id}
        try:
            with self._lock:
                self.vector_store.delete(where=where)
        except Exception as e:
            logger.warning(f"delete_session_memory failed: {e}")


if __name__ == "__main__":
    vs = VectorStoreService()
    loaded_count, available_count = vs.load_document()
    if available_count > 0:
        print("加载成功")
    else:
        print("加载失败")
