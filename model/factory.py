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


def _resolve_enable_thinking(override: dict | None = None) -> bool:
    """思考模式开关，默认关闭。优先级：用户配置 > .env ENABLE_THINKING > YAML。

    qwen3 系混合推理模型在 DashScope 兼容端点默认开思考：意图分类这类机械
    小任务实测被推理 token 拖到 2.5~7s（答案仅 4 token、思考 400~1000+，
    2026-09-03 差分实验实测，关思考后 0.43s）。故平台默认关，需要的用户在
    设置页 / .env 显式打开。override 值为 None（用户未设置）时回落下层配置。
    """
    override = override or {}
    raw = override.get("llm_enable_thinking")
    if raw is None:
        raw = os.environ.get("ENABLE_THINKING", rag_conf.get("enable_thinking", False))
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


# 模型调用兜底超时(秒)：防 LLM 请求无限挂起。
# 背景：live 观测到工具内模型调用无超时，请求挂死后 SSE producer 永久阻塞
# （2026-09-03，3 个 quick 并行后 7min+ 无日志、前端超时）。取值需大于当前
# 最慢的正常调用——SQLAgent 生成 SQL 实测 ~105s，故取 180s 作兜底上限；
# 正常请求不会触达，仅切断真正挂死的调用。
LLM_REQUEST_TIMEOUT_S = 180


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
        # enable_thinking 随请求体下发（DashScope 兼容端点参数）：默认关闭，
        # 见 _resolve_enable_thinking。第三方网关不识别该参数时一般会忽略。
        return ChatOpenAI(model=model_name, api_key=api_key or None,
                          base_url=base_url, streaming=True,
                          request_timeout=LLM_REQUEST_TIMEOUT_S,
                          extra_body={"enable_thinking": _resolve_enable_thinking(override)})
    # 未设 base_url -> ChatTongyi（DashScope）：传入用户 key，空则回退到 DASHSCOPE_API_KEY 环境变量
    # 显式 streaming=True 与 ChatOpenAI 路径(104 行)对齐;阶段 1.1 改造。
    # ChatTongyi 无 request_timeout 字段（langchain_community 1.3.x），
    # 超时兜底依赖底层 dashscope SDK 默认行为；live 走 OpenAI 兼容端点即 ChatOpenAI 路径。
    # 思考模式旋钮仅 ChatOpenAI 路径支持（extra_body）；ChatTongyi 分支暂不传。
    if api_key:
        return ChatTongyi(model=model_name, dashscope_api_key=api_key, streaming=True)
    return ChatTongyi(model=model_name, streaming=True)


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


def get_embed_model_name(user_id: str | None = None) -> str:
    """当前 user 解析到的向量模型名（用户配置 > .env > YAML），与 get_embed_model 同解析。"""
    return _resolve_embedding_model_name(_load_user_override(user_id))


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
