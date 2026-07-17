import os
from abc import ABC, abstractmethod
from typing import Optional
from langchain_community.chat_models.tongyi import BaseChatModel
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_core.embeddings import Embeddings
from langchain_community.chat_models import ChatTongyi

# ── 方案C：加载 .env 环境变量（DASHSCOPE_API_KEY 等）──
# 必须在实例化模型之前执行：优先从 .env 读取，但不覆盖已存在的环境变量。
try:
    from dotenv import load_dotenv
    # .env 位于项目根目录（agent/），即本文件上两级目录
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_env_path, override=False)
except ImportError:
    # python-dotenv 未安装时回退：依赖系统环境变量 DASHSCOPE_API_KEY
    pass

try:
    from agent.utils.config_handler import rag_conf
except ModuleNotFoundError:
    from utils.config_handler import rag_conf


def _model_name(key: str) -> str:
    """优先取环境变量，回退到 config/rag.yml 的配置。"""
    return os.environ.get(key) or rag_conf[key]


class BaseModelFactory(ABC):
    @abstractmethod
    def generator(self) -> Optional[Embeddings | BaseChatModel]:
        pass


class ChatModelFactory(BaseModelFactory):
    def generator(self) -> Optional[Embeddings | BaseChatModel]:
        # 大语言模型（通义千问）：名称取 CHAT_MODEL_NAME 环境变量或 rag.yml
        return ChatTongyi(model=_model_name("chat_model_name"))

class EmbeddingsFactory(BaseModelFactory):
    def generator(self) -> Optional[Embeddings | BaseChatModel]:
        # 向量嵌入模型：名称取 EMBEDDING_MODEL_NAME 环境变量或 rag.yml
        return DashScopeEmbeddings(model=_model_name("embedding_model_name"))

chat_model = ChatModelFactory().generator()
embed_model = EmbeddingsFactory().generator()
