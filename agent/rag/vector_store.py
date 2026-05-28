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
    from agent.utils.file_handler import text_loader, pdf_loader
    from agent.utils.logger_handler import logger
    from agent.utils.path_tool import get_abs_path
    from agent.utils.config_handler import chroma_conf
    from agent.model.factory import embed_model
except ModuleNotFoundError:
    from utils.file_handler import listdir_with_allowed_type, get_file_md5_hex
    from utils.file_handler import text_loader, pdf_loader
    from utils.logger_handler import logger
    from utils.path_tool import get_abs_path
    from utils.config_handler import chroma_conf
    from model.factory import embed_model


class VectorStoreService:
    def __init__(self,config_path="config/rag.yml"):
        self.config_path = config_path
        self.vector_store = Chroma(
            collection_name=chroma_conf["collection_name"],
            embedding_function=embed_model,
            persist_directory=get_abs_path(chroma_conf["persist_directory"]),
        )
        self.spliter = RecursiveCharacterTextSplitter(
            chunk_size=chroma_conf["chunk_size"],
            chunk_overlap=chroma_conf["chunk_overlap"],
            separators=chroma_conf["separators"],
            length_function=len,
        )

    def get_retriver(self):
        return self.vector_store.as_retriever(search_kwargs={"k":chroma_conf["k"]})

    def load_document(self):
        loaded_count = 0
        available_count = 0


        def check_md5_hex(md5_for_check:str):
            md5_store_path = get_abs_path(chroma_conf["md5_hex_store"])
            if not os.path.exists(md5_store_path):
                open(md5_store_path, "w",encoding="utf-8").close()
                return False

            with open(md5_store_path, "r",encoding="utf-8") as f:
                for line in f.readlines():
                    line = line.strip()
                    if line == md5_for_check:
                        return True
                return False

        def save_md5_hex(md5_for_check:str):
            with open(get_abs_path(chroma_conf["md5_hex_store"]), "a",encoding="utf-8") as f:
                f.write(md5_for_check+"\n")

        def get_file_documents(read_path: str):
            if read_path.endswith("txt"):
                return text_loader(read_path)

            if read_path.endswith("pdf"):
                return pdf_loader(read_path)

            return []

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
            if check_md5_hex(md5_hex):
                logger.info(f"加载知识库:{path}已存在知识库")
                available_count += 1
                continue
            try:
                documents: list[Document] = get_file_documents(path)

                if not documents:
                    logger.warning(f"加载知识库：{path}内无有效内容")
                    continue
                split_document = self.spliter.split_documents(documents)
                if not split_document:
                    logger.warning(f"{path}分片后无有效内容")
                    continue

                self.vector_store.add_documents(split_document)
                save_md5_hex(md5_hex)
                logger.info(f"{path}加载成功")
                loaded_count += 1
                available_count += 1
            except Exception as e:
                logger.error(f"{path}加载失败:{str(e)}",exc_info=True)
                continue

        return loaded_count, available_count


if __name__ == "__main__":
    vs = VectorStoreService()
    loaded_count, available_count = vs.load_document()
    if available_count > 0:
        print("加载成功")
    else:
        print("加载失败")
