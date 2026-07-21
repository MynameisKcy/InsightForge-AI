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
    """优先取环境变量，回退到 config/rag.yml 的配置（向后兼容默认路径）。"""
    return os.environ.get(key) or rag_conf[key]


# ── 用户级配置覆盖 + 热重载（需求①） ──
# 优先级：用户设置页配置 > .env 环境变量 > YAML 默认
import threading
_config_lock = threading.Lock()
_config_version = 0            # 每次 reload bump，getter 据此判断是否重建
_user_settings_override = {}  # {llm_model_name, embedding_model_name, llm_api_key}

_chat_model_cache = None
_chat_model_version = -1
_embed_model_cache = None
_embed_model_version = -1


def _current_user_id() -> str:
    """从 request_context 取当前 user_id；取不到返回 'default'。"""
    try:
        from utils.request_context import get_user_id
        return get_user_id()
    except Exception:
        pass
    return "default"


def _load_user_override(user_id: str) -> dict:
    """读取该用户的配置覆盖；失败/无配置返回空 dict。"""
    try:
        from database.user_settings_db import user_settings_db
        data = user_settings_db.get(user_id)
        if data:
            return data
    except Exception:
        pass
    return {}


def _resolve_chat_model_name() -> str:
    """优先级：用户配置 > .env > YAML。"""
    if _user_settings_override.get("llm_model_name"):
        return _user_settings_override["llm_model_name"]
    return os.environ.get("CHAT_MODEL_NAME") or rag_conf["chat_model_name"]


def _resolve_embedding_model_name() -> str:
    if _user_settings_override.get("embedding_model_name"):
        return _user_settings_override["embedding_model_name"]
    return os.environ.get("EMBEDDING_MODEL_NAME") or rag_conf["embedding_model_name"]


def _resolve_api_key() -> str:
    if _user_settings_override.get("llm_api_key"):
        return _user_settings_override["llm_api_key"]
    return os.environ.get("DASHSCOPE_API_KEY", "")


def reload_model_config(user_id: str) -> None:
    """配置保存后调用：重新加载该用户配置并 bump 版本号，触发热重载。"""
    global _config_version, _user_settings_override
    with _config_lock:
        _user_settings_override = _load_user_override(user_id)
        _config_version += 1


def get_chat_model():
    """getter：版本变化则重建实例；并发安全。"""
    global _chat_model_cache, _chat_model_version
    with _config_lock:
        if _chat_model_version != _config_version:
            _chat_model_cache = ChatTongyi(model=_resolve_chat_model_name())
            _chat_model_version = _config_version
        return _chat_model_cache


def get_embed_model():
    """getter：版本变化则重建实例；并发安全。"""
    global _embed_model_cache, _embed_model_version
    with _config_lock:
        if _embed_model_version != _config_version:
            _embed_model_cache = DashScopeEmbeddings(model=_resolve_embedding_model_name())
            _embed_model_version = _config_version
        return _embed_model_cache


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


# 向后兼容：保留模块级单例（用默认配置），未改造的旧代码仍可用
chat_model = ChatModelFactory().generator()
embed_model = EmbeddingsFactory().generator()
