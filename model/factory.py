import os
from abc import ABC, abstractmethod

from langchain_community.chat_models import ChatTongyi
from langchain_community.chat_models.tongyi import BaseChatModel
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_core.embeddings import Embeddings

# ── 方案C：加载 .env 环境变量（DASHSCOPE_API_KEY 等）──
# 必须在实例化模型之前执行：优先从 .env 读取，但不覆盖已存在的环境变量。
try:
    from dotenv import load_dotenv
    # .env 位于项目根目录，即本文件上两级目录
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_env_path, override=False)
except ImportError:
    # python-dotenv 未安装时回退：依赖系统环境变量 DASHSCOPE_API_KEY
    pass

from utils.config_handler import rag_conf


def _model_name(key: str) -> str:
    """优先取环境变量，回退到 config/rag.yml 的配置（向后兼容默认路径）。"""
    return os.environ.get(key) or rag_conf[key]


# ── 用户级配置 + 每用户模型缓存（多用户隔离） ──
# 优先级：用户设置页配置 > .env 环境变量 > YAML 默认
# 模型按 user_id 缓存：各用户用自己的 LLM 配置，互不泄漏。
# reload_model_config(user_id) 在配置保存后失效该用户缓存，下次取用时按新配置重建。
import threading

_config_lock = threading.Lock()
_chat_model_cache = {}   # user_id -> BaseChatModel
_embed_model_cache = {}  # user_id -> Embeddings


def _current_user_id() -> str:
    """从 request_context 取当前 user_id；取不到返回 'default'。"""
    try:
        from utils.request_context import get_user_id
        return get_user_id()
    except Exception:
        pass
    return "default"


def _load_user_override(user_id: str) -> dict:
    """读取该用户的配置覆盖；失败/无配置返回空 dict。user_id 为空时直接返回 {}，不触 DB。"""
    if not user_id:
        return {}
    try:
        from database.user_settings_db import user_settings_db
        data = user_settings_db.get(user_id)
        if data:
            return data
    except Exception:
        pass
    return {}


def _resolve_chat_model_name(override: dict | None = None) -> str:
    """优先级：用户配置 > .env > YAML。"""
    override = override or {}
    if override.get("llm_model_name"):
        return override["llm_model_name"]
    return os.environ.get("CHAT_MODEL_NAME") or rag_conf["chat_model_name"]


def _resolve_embedding_model_name(override: dict | None = None) -> str:
    override = override or {}
    if override.get("embedding_model_name"):
        return override["embedding_model_name"]
    return os.environ.get("EMBEDDING_MODEL_NAME") or rag_conf["embedding_model_name"]


def _resolve_api_key(override: dict | None = None) -> str:
    override = override or {}
    if override.get("llm_api_key"):
        return override["llm_api_key"]
    return os.environ.get("DASHSCOPE_API_KEY", "")


def _resolve_base_url(override: dict | None = None) -> str:
    """优先级：用户配置 > 环境变量 LLM_BASE_URL > 空（空则用默认 ChatTongyi/千问）。"""
    override = override or {}
    if override.get("llm_base_url"):
        return override["llm_base_url"]
    return os.environ.get("LLM_BASE_URL", "")


def _build_chat_model(user_id: str | None = None):
    """根据是否配置 base_url 选择实现：
    - 设了 base_url → ChatOpenAI 接 OpenAI 兼容端点（不限千问，如第三方代理/自建网关）
    - 未设 base_url → ChatTongyi（DashScope 通义千问，默认路径）
    """
    override = _load_user_override(user_id)
    base_url = _resolve_base_url(override)
    model_name = _resolve_chat_model_name(override)
    api_key = _resolve_api_key(override)
    if base_url:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model_name, api_key=api_key or None,
                          base_url=base_url, streaming=True)
    # 未设 base_url -> ChatTongyi（DashScope）：传入用户 key，空则回退到 DASHSCOPE_API_KEY 环境变量
    if api_key:
        return ChatTongyi(model=model_name, dashscope_api_key=api_key)
    return ChatTongyi(model=model_name)


def reload_model_config(user_id: str) -> None:
    """配置保存后调用：失效该用户的模型缓存，下次取用时按新配置重建。

    配合 api.deps._invalidate_user_agents(user_id) 一并丢弃该用户的 Agent 实例，
    使新配置在下次请求时真正生效（Agent 不再持有旧模型）。
    """
    with _config_lock:
        _chat_model_cache.pop(user_id, None)
        _embed_model_cache.pop(user_id, None)


def get_chat_model(user_id: str | None = None):
    """getter：按 user_id 缓存模型实例；并发安全。

    user_id=None -> 默认配置（.env/YAML），供未改造的旧代码与 RAG 使用。
    每个 user_id 首次取用时构建并缓存，用户间互不污染（修复旧单例泄漏问题）。
    """
    key = user_id or "__default__"
    with _config_lock:
        if key not in _chat_model_cache:
            _chat_model_cache[key] = _build_chat_model(user_id)
        return _chat_model_cache[key]


def get_chat_model_name(user_id: str | None = None) -> str:
    """当前 user 解析到的聊天模型名（用户配置 > .env > YAML）。

    供记忆层按模型查上下文窗口（ADR-0003 Phase 2），与 get_chat_model 用同一解析逻辑。
    """
    return _resolve_chat_model_name(_load_user_override(user_id))


def get_embed_model(user_id: str | None = None):
    """getter：按 user_id 缓存向量模型实例；并发安全。"""
    key = user_id or "__default__"
    with _config_lock:
        if key not in _embed_model_cache:
            _embed_model_cache[key] = DashScopeEmbeddings(
                model=_resolve_embedding_model_name(_load_user_override(user_id)))
        return _embed_model_cache[key]


class BaseModelFactory(ABC):
    @abstractmethod
    def generator(self) -> Embeddings | BaseChatModel | None:
        pass


class ChatModelFactory(BaseModelFactory):
    def generator(self) -> Embeddings | BaseChatModel | None:
        # 大语言模型（通义千问）：名称取 CHAT_MODEL_NAME 环境变量或 rag.yml
        return ChatTongyi(model=_model_name("chat_model_name"))


class EmbeddingsFactory(BaseModelFactory):
    def generator(self) -> Embeddings | BaseChatModel | None:
        # 向量嵌入模型：名称取 EMBEDDING_MODEL_NAME 环境变量或 rag.yml
        return DashScopeEmbeddings(model=_model_name("embedding_model_name"))


# 向后兼容：保留模块级单例（用默认配置），未改造的旧代码仍可用
chat_model = ChatModelFactory().generator()
embed_model = EmbeddingsFactory().generator()
