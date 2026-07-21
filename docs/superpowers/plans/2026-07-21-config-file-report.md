# InsightForge 配置管理 + 文件管理 + 报告图表 改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 InsightForge 上新增「用户级配置管理（热重载）」「统一文件管理（文本/表格分轨）」「文本报告 + 混合调度」三项能力，复用现有 Chroma/DuckDB/Plotly/ExportAgent 管道。

**Architecture:** 配置存 SQLite 绑定 user_id（API Key Fernet 加密）；`factory.py` 单例改 getter+版本号热重载；文件按扩展名分轨（文本→Chroma，表格→DuckDB），`GET /api/files` 统一列表；新增 `DocumentReportAgent` 做文本报告，ReactAgent 加 `list_user_files`/`document_report` 两个 `@tool`，LLM 自动选文件/管道。

**Tech Stack:** Python, FastAPI, SQLite, ChromaDB, DuckDB, LangChain/LangGraph, DashScope (ChatTongyi/DashScopeEmbeddings/gte-rerank-v2), Plotly, cryptography (Fernet), pytest.

## Global Constraints

- 所有命令在 `AnalysisAgent` conda 环境运行：`eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent`
- 路径相对 `agent/` 目录；`get_abs_path()` 解析相对路径。
- LLM 调用在测试中一律 mock，离线可跑。
- 沿用安全加固：所有用户数据绑定 `owner_user_id`；DuckDB 标识符用 `safe_ident()`；SQL 经 sqlglot AST 校验。
- 沿用 SSE 约定：`[ERROR:text]`/`[CHART:url]`/`[DONE]`；前端错误用 toast，不白屏。
- 沿用双 import 兼容写法（`try: from agent.x import y / except ModuleNotFoundError: from x import y`）。
- 频繁提交，每个 Task 一次 commit。
- DashScope rerank 模型必须用 `gte-rerank-v2`。

参考设计文档：`docs/superpowers/specs/2026-07-21-config-file-report-design.md`

---

## 文件结构

**新建：**
- `agent/database/user_settings_db.py` — `UserSettingsDB`，`user_settings` 表 + Fernet 加解密 + 掩码。
- `agent/agents/document_report_agent.py` — `DocumentReportAgent(BaseAgent)`，文本文件→结构化 Markdown 报告。
- `agent/prompts/document_report.txt` — DocumentReportAgent 系统提示词。
- `tests/test_user_settings_db.py`、`tests/test_factory_getter.py`、`tests/test_settings_api.py`、`tests/test_config_priority.py`、`tests/test_unified_files_api.py`、`tests/test_document_report_agent.py`、`tests/test_list_user_files_tool.py`。

**修改：**
- `agent/model/factory.py` — 加 `get_chat_model()/get_embed_model()` getter + 版本号 + `reload_model_config(user_id)`；优先级 用户配置 > .env > YAML。
- `agent/rag/vector_store.py:21,28`、`agent/rag/rag_service.py:23,28`、`agent/agents/base.py:15`、`agent/agent/react_agent.py:15,23,35` — 改调 getter。
- `agent/agent/tools/agent_tools.py` — 加 `list_user_files`、`document_report` 两个 `@tool`。
- `agent/api/fastapi_server.py` — 加 `GET/POST /api/settings`、`GET /api/settings/status`、`GET /api/files`；侧边栏加「账号设置」「文件管理（统一）」面板；登录后查 status 弹提示。
- `agent/requirements.txt` — 加 `cryptography`。

---

## Task 1: UserSettingsDB — 用户配置存储 + Fernet 加密

**Files:**
- Create: `agent/database/user_settings_db.py`
- Test: `tests/test_user_settings_db.py`

**Interfaces:**
- Produces: `UserSettingsDB` 类，实例 `user_settings_db`（模块级单例）。
  - `get(user_id: str) -> dict | None`：返回明文配置（API Key 解密）；未配置返回 `None`。
  - `upsert(user_id: str, settings: dict) -> None`：写入/更新；`llm_api_key` 字段加密存储。
  - `has(user_id: str) -> bool`。
  - `get_masked(user_id: str) -> dict | None`：返回掩码版（`llm_api_key` → `sk-****4213`），供 API 返回前端。
- Consumes: `utils.path_tool.get_abs_path`、`utils.logger_handler.logger`、环境变量 `INSIGHTFORGE_SETTINGS_KEY`（缺失则生成并写 `.env`）。

- [ ] **Step 1: Write the failing test**

`tests/test_user_settings_db.py`:
```python
import os, sys, tempfile
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
sys.path.insert(0, PROJECT_ROOT)

import agent.database.user_settings_db as usd_mod

def _fresh_db(tmp_path):
    """给模块换一个临时 DB_PATH，避免污染真实库。"""
    usd_mod.DB_PATH = str(tmp_path / "user_settings.db")
    usd_mod._ensure_db()
    usd_mod._init_db()
    return usd_mod.UserSettingsDB()

def test_upsert_get_roundtrip(tmp_path):
    db = _fresh_db(tmp_path)
    db.upsert("u1", {"llm_api_key": "sk-caa0410b364d47f09774d0c3b2b64213",
                     "llm_model_name": "qwen-max",
                     "embedding_model_name": "text-embedding-v2",
                     "local_db_conn": "sqlite:///db.db"})
    got = db.get("u1")
    assert got["llm_api_key"] == "sk-caa0410b364d47f09774d0c3b2b64213"
    assert got["llm_model_name"] == "qwen-max"

def test_get_none_when_absent(tmp_path):
    db = _fresh_db(tmp_path)
    assert db.get("nope") is None
    assert db.has("nope") is False

def test_masked_hides_full_key(tmp_path):
    db = _fresh_db(tmp_path)
    db.upsert("u1", {"llm_api_key": "sk-caa0410b364d47f09774d0c3b2b64213",
                     "llm_model_name": "qwen-max"})
    masked = db.get_masked("u1")
    assert masked["llm_api_key"].startswith("sk-")
    assert "b2b64213" in masked["llm_api_key"]  # 末尾4位可见
    assert "caa0410b364d47f09774d0c3" not in masked["llm_api_key"]  # 中间不可见

def test_storage_is_encrypted(tmp_path):
    db = _fresh_db(tmp_path)
    db.upsert("u1", {"llm_api_key": "sk-secretkey123456",
                     "llm_model_name": "qwen-max"})
    # 直接读 SQLite 文件，明文不应出现
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "user_settings.db"))
    row = conn.execute("SELECT llm_api_key_enc FROM user_settings WHERE user_id=?", ("u1",)).fetchone()
    conn.close()
    assert "sk-secretkey123456" not in row[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_user_settings_db.py -v`
Expected: FAIL — `ModuleNotFoundError: agent.database.user_settings_db`

- [ ] **Step 3: Write minimal implementation**

`agent/database/user_settings_db.py`:
```python
"""
用户配置存储：每用户一行，API Key 用 Fernet 对称加密。
优先级（消费侧在 factory.getter 实现）：用户配置 > .env > YAML 默认。
"""
import os
import sys
import sqlite3

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

DB_PATH = get_abs_path("database/user_settings.db")

FIELDS = [
    "llm_api_key_enc", "llm_model_name", "embedding_model_name",
    "vector_db_host", "vector_db_port", "vector_db_collection",
    "vector_db_tenant", "local_db_conn", "updated_at",
]


def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def _get_fernet():
    """获取 Fernet：密钥来自环境变量 INSIGHTFORGE_SETTINGS_KEY；缺失则生成并写 .env。"""
    from cryptography.fernet import Fernet
    key = os.environ.get("INSIGHTFORGE_SETTINGS_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\nINSIGHTFORGE_SETTINGS_KEY={key}\n")
        os.environ["INSIGHTFORGE_SETTINGS_KEY"] = key
        logger.info("已生成并写入 INSIGHTFORGE_SETTINGS_KEY")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _init_db():
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        user_id TEXT PRIMARY KEY,
        llm_api_key_enc TEXT,
        llm_model_name TEXT,
        embedding_model_name TEXT,
        vector_db_host TEXT,
        vector_db_port TEXT,
        vector_db_collection TEXT,
        vector_db_tenant TEXT,
        local_db_conn TEXT,
        updated_at TEXT
    )""")
    conn.commit()
    conn.close()


def mask_api_key(key: str) -> str:
    """掩码：保留前缀与末4位，中间用 ****。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


class UserSettingsDB:
    def __init__(self):
        _init_db()

    def _connect(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def has(self, user_id: str) -> bool:
        conn = self._connect()
        row = conn.execute("SELECT user_id FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        return row is not None

    def get(self, user_id: str) -> dict | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        if row is None:
            return None
        f = _get_fernet()
        api_key = ""
        if row["llm_api_key_enc"]:
            try:
                api_key = f.decrypt(row["llm_api_key_enc"].encode()).decode()
            except Exception as e:
                logger.warning(f"API Key 解密失败: {e}")
        return {
            "llm_api_key": api_key,
            "llm_model_name": row["llm_model_name"],
            "embedding_model_name": row["embedding_model_name"],
            "vector_db_host": row["vector_db_host"],
            "vector_db_port": row["vector_db_port"],
            "vector_db_collection": row["vector_db_collection"],
            "vector_db_tenant": row["vector_db_tenant"],
            "local_db_conn": row["local_db_conn"],
            "updated_at": row["updated_at"],
        }

    def get_masked(self, user_id: str) -> dict | None:
        data = self.get(user_id)
        if data is None:
            return None
        data["llm_api_key"] = mask_api_key(data.get("llm_api_key", ""))
        return data

    def upsert(self, user_id: str, settings: dict) -> None:
        from datetime import datetime
        f = _get_fernet()
        api_key = settings.get("llm_api_key", "")
        enc = f.encrypt(api_key.encode()).decode() if api_key else None
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._connect()
        conn.execute(f"""INSERT INTO user_settings
            (user_id, llm_api_key_enc, llm_model_name, embedding_model_name,
             vector_db_host, vector_db_port, vector_db_collection,
             vector_db_tenant, local_db_conn, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              llm_api_key_enc=excluded.llm_api_key_enc,
              llm_model_name=excluded.llm_model_name,
              embedding_model_name=excluded.embedding_model_name,
              vector_db_host=excluded.vector_db_host,
              vector_db_port=excluded.vector_db_port,
              vector_db_collection=excluded.vector_db_collection,
              vector_db_tenant=excluded.vector_db_tenant,
              local_db_conn=excluded.local_db_conn,
              updated_at=excluded.updated_at""",
            (user_id, enc, settings.get("llm_model_name"),
             settings.get("embedding_model_name"), settings.get("vector_db_host"),
             settings.get("vector_db_port"), settings.get("vector_db_collection"),
             settings.get("vector_db_tenant"), settings.get("local_db_conn"), now))
        conn.commit()
        conn.close()


user_settings_db = UserSettingsDB()
```

- [ ] **Step 4: Add cryptography to requirements**

`agent/requirements.txt` 末尾追加一行（若已存在则跳过）:
```
cryptography
```
Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && pip install cryptography`

- [ ] **Step 5: Run test to verify it passes**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_user_settings_db.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
cd C:/Users/86131/Multi-Agent-Data-Analysis-System
git add agent/database/user_settings_db.py tests/test_user_settings_db.py agent/requirements.txt
git commit -m "feat(s1): add UserSettingsDB with Fernet-encrypted API key storage"
```

---

## Task 2: factory getter 热重载 + 配置优先级

**Files:**
- Modify: `agent/model/factory.py`
- Modify: `agent/rag/vector_store.py:21,28`、`agent/rag/rag_service.py:23,28`、`agent/agents/base.py:15,24`、`agent/agent/react_agent.py:15,23,35`
- Test: `tests/test_factory_getter.py`、`tests/test_config_priority.py`

**Interfaces:**
- Produces:
  - `get_chat_model()` -> ChatTongyi 实例（按当前用户配置或默认）
  - `get_embed_model()` -> DashScopeEmbeddings 实例
  - `reload_model_config(user_id: str)` -> bump 版本号，下次 getter 重建
  - 保留 `chat_model`/`embed_model` 模块级单例（向后兼容，首次用默认实例化）
- Consumes: `UserSettingsDB.get(user_id)`（来自 Task 1）、`request_context` 取当前 user_id。

- [ ] **Step 1: Write the failing test**

`tests/test_factory_getter.py`:
```python
import os, sys
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
sys.path.insert(0, PROJECT_ROOT)

import agent.model.factory as fac

def test_getter_returns_model_instance():
    m = fac.get_chat_model()
    assert m is not None

def test_reload_returns_new_instance_after_bump(monkeypatch):
    a = fac.get_chat_model()
    # 模拟一次配置保存触发 reload
    monkeypatch.setattr(fac, "_config_version", fac._config_version + 1)
    monkeypatch.setattr(fac, "_user_settings_override", {"llm_model_name": "qwen-max"})
    b = fac.get_chat_model()
    assert b is not a  # 版本变了 → 新实例

def test_concurrent_reload_keeps_old_ref(monkeypatch):
    old = fac.get_chat_model()
    # 不 bump 版本，override 不变 → 同一实例
    assert fac.get_chat_model() is old
```

`tests/test_config_priority.py`:
```python
import os, sys
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
sys.path.insert(0, PROJECT_ROOT)

import agent.model.factory as fac

def test_priority_user_over_env_over_yaml(monkeypatch):
    # 用户配置 = qwen-user, 环境变量 = qwen-env, YAML = qwen-yaml
    monkeypatch.setenv("CHAT_MODEL_NAME", "qwen-env")
    monkeypatch.setattr(fac, "rag_conf", {"chat_model_name": "qwen-yaml"})
    monkeypatch.setattr(fac, "_user_settings_override", {"llm_model_name": "qwen-user"})
    assert fac._resolve_chat_model_name() == "qwen-user"

def test_fallback_to_env_when_no_user_setting(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL_NAME", "qwen-env")
    monkeypatch.setattr(fac, "rag_conf", {"chat_model_name": "qwen-yaml"})
    monkeypatch.setattr(fac, "_user_settings_override", {})
    assert fac._resolve_chat_model_name() == "qwen-env"

def test_fallback_to_yaml(monkeypatch):
    monkeypatch.delenv("CHAT_MODEL_NAME", raising=False)
    monkeypatch.setattr(fac, "rag_conf", {"chat_model_name": "qwen-yaml"})
    monkeypatch.setattr(fac, "_user_settings_override", {})
    assert fac._resolve_chat_model_name() == "qwen-yaml"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_factory_getter.py tests/test_config_priority.py -v`
Expected: FAIL — `AttributeError: get_chat_model`

- [ ] **Step 3: Implement getter + 优先级解析**

替换 `agent/model/factory.py` 第 26-48 行为：

```python
def _model_name(key: str) -> str:
    """环境变量优先于 config/rag.yml（向后兼容默认路径）。"""
    return os.environ.get(key) or rag_conf[key]


# ── 用户级配置覆盖（热重载） ──
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
        from utils.request_context import get_request_context
        ctx = get_request_context()
        if ctx and ctx.user_id:
            return ctx.user_id
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
        return ChatTongyi(model=_model_name("chat_model_name"))


class EmbeddingsFactory(BaseModelFactory):
    def generator(self) -> Optional[Embeddings | BaseChatModel]:
        return DashScopeEmbeddings(model=_model_name("embedding_model_name"))


# 向后兼容：保留模块级单例（用默认配置），未改造的旧代码仍可用
chat_model = ChatModelFactory().generator()
embed_model = EmbeddingsFactory().generator()
```

- [ ] **Step 4: Run factory tests**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_factory_getter.py tests/test_config_priority.py -v`
Expected: PASS

- [ ] **Step 5: Switch singleton consumers to getter**

`agent/rag/vector_store.py` 第 21 行改为 `from agent.model.factory import get_embed_model`，第 28 行 `from model.factory import embed_model` → `from model.factory import get_embed_model`；第 50 行 `embedding_function=embed_model` → `embedding_function=get_embed_model()`。

`agent/rag/rag_service.py` 第 23 行 `from agent.model.factory import chat_model` → `from agent.model.factory import get_chat_model`；第 28 行同理；类内用到 `chat_model` 处改为 `get_chat_model()`（搜索 `self.model =` 或 `chat_model` 引用点替换）。

`agent/agents/base.py` 第 15 行 `from model.factory import chat_model` → `from model.factory import get_chat_model`；第 24 行 `self.model = chat_model` → `self.model = get_chat_model()`。

`agent/agent/react_agent.py` 第 15 行与第 23 行 import 改 `get_chat_model`；第 35 行 `model=chat_model` → `model=get_chat_model()`。

- [ ] **Step 6: Smoke-run existing tests to confirm no regression**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/ -v`
Expected: 既有测试不新增失败（与改造前一致或更好）。

- [ ] **Step 7: Commit**

```bash
cd C:/Users/86131/Multi-Agent-Data-Analysis-System
git add agent/model/factory.py agent/rag/vector_store.py agent/rag/rag_service.py agent/agents/base.py agent/agent/react_agent.py tests/test_factory_getter.py tests/test_config_priority.py
git commit -m "feat(s2): hot-reload LLM/embed models via versioned getters, user config > env > yaml"
```

---

## Task 3: 设置 API + 前端「账号设置」面板

**Files:**
- Modify: `agent/api/fastapi_server.py`（新增 3 个端点 + 侧边栏面板 + 登录后 status 检查）
- Test: `tests/test_settings_api.py`

**Interfaces:**
- Produces:
  - `GET /api/settings` -> `{configured, settings(masked)} | {configured:false}`
  - `POST /api/settings` (body: 配置 dict) -> `{ok:true}`；保存后调 `reload_model_config(user_id)`
  - `GET /api/settings/status` -> `{configured: bool}`
- Consumes: `UserSettingsDB`（Task 1）、`reload_model_config`（Task 2）、`_get_user_id(request)`（已有，行 106）。

- [ ] **Step 1: Write the failing test**

`tests/test_settings_api.py`:
```python
import os, sys
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient
import agent.api.fastapi_server as srv
import agent.database.user_settings_db as usd_mod

def _patched_user_id(request):
    return "test_user"

def _fresh_settings(tmp_path):
    usd_mod.DB_PATH = str(tmp_path / "u.db")
    usd_mod._ensure_db(); usd_mod._init_db()
    import importlib
    importlib.reload(usd_mod)
    srv.user_settings_db = usd_mod.user_settings_db

def test_status_unconfigured(tmp_path, monkeypatch):
    _fresh_settings(tmp_path)
    monkeypatch.setattr(srv, "_get_user_id", _patched_user_id)
    client = TestClient(srv.app)
    r = client.get("/api/settings/status")
    assert r.status_code == 200
    assert r.json()["configured"] is False

def test_save_then_masked_get(tmp_path, monkeypatch):
    _fresh_settings(tmp_path)
    monkeypatch.setattr(srv, "_get_user_id", _patched_user_id)
    client = TestClient(srv.app)
    client.post("/api/settings", json={"llm_api_key": "sk-secretkey123456", "llm_model_name": "qwen-max"})
    r = client.get("/api/settings")
    body = r.json()
    assert body["configured"] is True
    assert "****" in body["settings"]["llm_api_key"]
    assert "secretkey123456" not in body["settings"]["llm_api_key"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_settings_api.py -v`
Expected: FAIL — 路由不存在（404）

- [ ] **Step 3: Add settings endpoints**

在 `agent/api/fastapi_server.py` 知识库端点附近（约 1854 行 `GET /api/knowledge/files` 之前）插入：

```python
# ── 账号设置（需求①） ──
try:
    from database.user_settings_db import user_settings_db
except ModuleNotFoundError:
    from agent.database.user_settings_db import user_settings_db

from model.factory import reload_model_config

@app.get("/api/settings/status")
async def get_settings_status(request: Request):
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"configured": False, "authed": False})
    return {"configured": user_settings_db.has(user_id), "authed": True}

@app.get("/api/settings")
async def get_settings(request: Request):
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"configured": False, "settings": None}, status_code=401)
    data = user_settings_db.get_masked(user_id)
    if data is None:
        return {"configured": False, "settings": None}
    return {"configured": True, "settings": data}

@app.post("/api/settings")
async def save_settings(request: Request):
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    body = await request.json()
    # 仅接受白名单字段
    allowed = {"llm_api_key", "llm_model_name", "embedding_model_name",
               "vector_db_host", "vector_db_port", "vector_db_collection",
               "vector_db_tenant", "local_db_conn"}
    cleaned = {k: v for k, v in body.items() if k in allowed}
    # 若前端回传掩码值（含 ****），则不覆盖已存的 key
    if "****" in str(cleaned.get("llm_api_key", "")):
        cleaned.pop("llm_api_key", None)
        existing = user_settings_db.get(user_id) or {}
        if existing.get("llm_api_key"):
            cleaned["llm_api_key"] = existing["llm_api_key"]
    try:
        user_settings_db.upsert(user_id, cleaned)
        reload_model_config(user_id)
        return {"ok": True}
    except Exception as e:
        logger.exception("保存配置失败")
        return JSONResponse({"ok": False, "error": f"保存失败: {e}"}, status_code=500)
```

- [ ] **Step 4: Run API tests**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_settings_api.py -v`
Expected: 2 PASS

- [ ] **Step 5: Add sidebar 「账号设置」 panel + 未配置提示**

在 `HTML_TEMPLATE`（约 260-1320 行）侧边栏「知识库」面板后追加一个折叠面板 `#settings-section`，含三组表单（LLM：API Key/模型名；向量：模型名/host/port/collection/tenant；数据库：连接串），API Key 输入框默认 type=password 并显示掩码占位，点「编辑」切换为明文 input。JS：进入 `/app` 后 `fetch('/api/settings/status')`，若 `configured=false` 显示顶部横幅「检测到尚未配置，点此前往账号设置」+ 侧边栏红点；保存按钮 `fetch('/api/settings', {method:POST, body:JSON.stringify(...)})` 成功后 toast「配置已生效」。

具体 HTML/JS 代码体量较大，实现时遵循现有 `ds-section`/`kb-section` 面板的结构与样式类名，保持一致。

- [ ] **Step 6: 手动冒烟**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m api.fastapi_server`
浏览器开 `http://localhost:8502`，登录后确认：未配置时弹提示；填配置保存后 toast；`GET /api/settings` 返回掩码。

- [ ] **Step 7: Commit**

```bash
cd C:/Users/86131/Multi-Agent-Data-Analysis-System
git add agent/api/fastapi_server.py tests/test_settings_api.py
git commit -m "feat(s3): settings API + sidebar account-settings panel with masked API key"
```

---

## Task 4: 统一文件列表 `GET /api/files`

**Files:**
- Modify: `agent/api/fastapi_server.py`（新增 `GET /api/files`）
- Test: `tests/test_unified_files_api.py`

**Interfaces:**
- Produces: `GET /api/files` -> `[{name, type: text|table, size, upload_time, status, source}]`，合并 Chroma 文本类 + DuckDB 表格类。
- Consumes: 现有 `GET /api/knowledge/files` 逻辑（VectorStoreService 列文件）、`GET /api/datasets` 逻辑（datasources_db 列表）。

- [ ] **Step 1: Write the failing test**

`tests/test_unified_files_api.py`:
```python
import os, sys
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient
import agent.api.fastapi_server as srv

def _patched_user_id(request):
    return "test_user"

def test_files_returns_list(monkeypatch):
    monkeypatch.setattr(srv, "_get_user_id", _patched_user_id)
    client = TestClient(srv.app)
    r = client.get("/api/files")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["files"], list)
    # 每项含必要字段
    for f in body["files"]:
        assert "name" in f and "type" in f and "status" in f
```

- [ ] **Step 2: Run to verify fail**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_unified_files_api.py -v`
Expected: FAIL — 404

- [ ] **Step 3: Implement `/api/files`**

在 `fastapi_server.py` 知识库端点区插入（合并两类；复用现有内部逻辑，不重写解析）：

```python
@app.get("/api/files")
async def list_all_files(request: Request):
    """统一文件列表：文本类（Chroma）+ 表格类（DuckDB 数据集）。"""
    user_id = await _get_user_id(request)
    files = []
    # 文本类
    try:
        for f in _get_vector_store().list_files(user_id):  # 见下方说明
            files.append({"name": f["name"], "type": "text", "size": f.get("size"),
                           "upload_time": f.get("upload_time"),
                           "status": f.get("status", "已完成"), "source": "chroma"})
    except Exception as e:
        logger.warning(f"列文本文件失败: {e}")
    # 表格类
    try:
        for d in datasources_db.list_datasets(user_id):  # 见下方说明
            files.append({"name": d["name"], "type": "table", "size": d.get("size"),
                           "upload_time": d.get("created_at"),
                           "status": "已完成", "source": "duckdb",
                           "table_name": d.get("table_name")})
    except Exception as e:
        logger.warning(f"列数据集失败: {e}")
    return {"files": files}
```

> 说明：`VectorStoreService.list_files(user_id)` 与 `DatasourcesDB.list_datasets(user_id)` 为现有方法或需补的小方法。先读 `rag/vector_store.py` 与 `database/datasources_db.py` 确认方法签名；若现有 `list_files`/列表方法返回字段不全或缺 user_id 过滤，在本 Task 内补齐（遵循 owner_user_id 隔离）。`_get_vector_store()` 单例已存在（行 92）。

- [ ] **Step 4: Run test**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_unified_files_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/86131/Multi-Agent-Data-Analysis-System
git add agent/api/fastapi_server.py agent/rag/vector_store.py agent/database/datasources_db.py tests/test_unified_files_api.py
git commit -m "feat(s4): unified file list API merging Chroma text + DuckDB table files"
```

---

## Task 5: DocumentReportAgent — 文本文件报告

**Files:**
- Create: `agent/agents/document_report_agent.py`
- Create: `agent/prompts/document_report.txt`
- Modify: `agent/utils/prompt_loader.py`（加载新 prompt，遵循现有模式）
- Test: `tests/test_document_report_agent.py`

**Interfaces:**
- Produces: `DocumentReportAgent(BaseAgent)`，方法 `run(file_path: str, question: str | None = None) -> dict`，返回 `{"markdown": str, "file": str}`。Markdown 含「摘要」「关键要点」「（可选）问答」三段。
- Consumes: `BaseAgent._call_llm`（已改 getter，Task 2）、`utils/file_handler` 加载器取文件文本。

- [ ] **Step 1: Write the failing test**

`tests/test_document_report_agent.py`:
```python
import os, sys
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from agents.document_report_agent import DocumentReportAgent

class _FakeBase:
    def _call_llm(self, messages):
        return "# 摘要\n本文档讲X。\n\n## 关键要点\n- 点1\n- 点2\n\n## 问答\n问:A"

def test_run_returns_markdown_with_sections(monkeypatch, tmp_path):
    txt = tmp_path / "note.txt"
    txt.write_text("示例内容" * 200, encoding="utf-8")
    agent = DocumentReportAgent()
    agent._call_llm = _FakeBase()._call_llm  # mock LLM
    out = agent.run(str(txt), question="要点是什么？")
    assert "摘要" in out["markdown"]
    assert "关键要点" in out["markdown"]
    assert "问答" in out["markdown"]
    assert out["file"] == str(txt)
```

- [ ] **Step 2: Run to verify fail**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_document_report_agent.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write prompt**

`agent/prompts/document_report.txt`:
```
你是一个文档分析助手。基于给定的文档内容，输出一份结构化 Markdown 报告，严格包含以下小节（用 Markdown 标题）：

# 摘要
（200 字以内概括文档主旨）

## 关键要点
（3-6 条要点，每条以 `- ` 开头）

## 问答
（若用户提供了 question，回答该问题；否则简要给出文档可能被问到的 2 个核心问答）

仅输出 Markdown 正文，不要输出额外解释。
```

- [ ] **Step 4: Write the agent**

`agent/agents/document_report_agent.py`:
```python
"""
DocumentReportAgent：针对文本类文件（PDF/Word/TXT/MD）生成
结构化报告（摘要 + 关键要点 + 可选问答），输出 Markdown。
"""
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from agents.base import BaseAgent

try:
    from agent.utils.file_handler import pdf_loader, docx_loader, text_loader, markdown_loader
    from agent.utils.prompt_loader import load_document_report_prompts
except ModuleNotFoundError:
    from utils.file_handler import pdf_loader, docx_loader, text_loader, markdown_loader
    from utils.prompt_loader import load_document_report_prompts


def _load_text(file_path: str) -> str:
    lower = file_path.lower()
    if lower.endswith(".txt") or lower.endswith(".md"):
        docs = text_loader(file_path) if lower.endswith(".txt") else markdown_loader(file_path)
    elif lower.endswith(".pdf"):
        docs = pdf_loader(file_path)
    elif lower.endswith(".docx"):
        docs = docx_loader(file_path)
    else:
        return ""
    return "\n".join(getattr(d, "page_content", str(d)) for d in (docs or []))[:8000]


class DocumentReportAgent(BaseAgent):
    name = "document_report"

    def run(self, file_path: str, question: str | None = None) -> dict:
        content = _load_text(file_path)
        if not content:
            return {"markdown": f"无法解析文件：{file_path}", "file": file_path}
        sys_prompt = load_document_report_prompts()
        user_msg = f"文档内容：\n{content}\n\n"
        if question:
            user_msg += f"用户问题：{question}"
        else:
            user_msg += "未提供具体问题，请按模板输出。"
        md = self._call_llm([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ])
        return {"markdown": md, "file": file_path}
```

- [ ] **Step 5: Wire prompt loader**

在 `agent/utils/prompt_loader.py` 加 `load_document_report_prompts()`，读取 `prompts/document_report.txt`，遵循该文件现有 `load_system_prompts()` 的实现模式（读取路径 + 编码）。若 `prompts.yml` 有 prompt 路径表，则在其中登记 `document_report` 键。

- [ ] **Step 6: Run test**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_document_report_agent.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
cd C:/Users/86131/Multi-Agent-Data-Analysis-System
git add agent/agents/document_report_agent.py agent/prompts/document_report.txt agent/utils/prompt_loader.py tests/test_document_report_agent.py
git commit -m "feat(s5): DocumentReportAgent for text-file summary/keypoints/qa reports"
```

---

## Task 6: ReactAgent 工具 — list_user_files + document_report

**Files:**
- Modify: `agent/agent/tools/agent_tools.py`（加两个 `@tool`）
- Modify: `agent/agent/react_agent.py`（注册到 tools 列表）
- Test: `tests/test_list_user_files_tool.py`

**Interfaces:**
- Produces:
  - `list_user_files() -> str`：返回当前用户文件清单字符串（JSON），含 name/type/table_name/status。
  - `document_report(file_path: str, question: str = "") -> str`：调 `DocumentReportAgent.run`，返回 Markdown。
- Consumes: `request_context` 取 user_id、`/api/files` 同源逻辑（直接复用 datasources_db + vector_store list，避免 HTTP 自调用）、`DocumentReportAgent`（Task 5）。

- [ ] **Step 1: Write the failing test**

`tests/test_list_user_files_tool.py`:
```python
import os, sys, json
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from agent.tools.agent_tools import list_user_files

def test_list_user_files_returns_json(monkeypatch):
    monkeypatch.setattr("agent.tools.agent_tools._current_user_id", lambda: "u1")
    # 桩 list 函数
    monkeypatch.setattr("agent.tools.agent_tools._list_text_files", lambda u: [{"name":"a.pdf","status":"已完成"}])
    monkeypatch.setattr("agent.tools.agent_tools._list_table_files", lambda u: [{"name":"sales.csv","table_name":"sales"}])
    out = list_user_files.invoke({})
    data = json.loads(out)
    assert any(f["name"] == "a.pdf" for f in data)
    assert any(f["name"] == "sales.csv" for f in data)
```

- [ ] **Step 2: Run to verify fail**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_list_user_files_tool.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Add tools**

在 `agent/agent/tools/agent_tools.py` 末尾追加：

```python
import json as _json

try:
    from utils.request_context import get_request_context
except ModuleNotFoundError:
    from agent.utils.request_context import get_request_context


def _current_user_id() -> str:
    try:
        ctx = get_request_context()
        if ctx and ctx.user_id:
            return ctx.user_id
    except Exception:
        pass
    return "default"


def _list_text_files(user_id: str):
    try:
        from rag.vector_store import VectorStoreService
        return VectorStoreService().list_files(user_id)
    except Exception as e:
        return [{"error": str(e)}]


def _list_table_files(user_id: str):
    try:
        from database.datasources_db import datasources_db
        return datasources_db.list_datasets(user_id)
    except Exception as e:
        return [{"error": str(e)}]


@tool(description="列出当前用户已上传的所有文件（文本类与表格类），含文件名、类型、表名、状态，供选择文件时使用。返回 JSON 字符串。")
def list_user_files() -> str:
    uid = _current_user_id()
    files = []
    for f in _list_text_files(uid):
        files.append({"name": f.get("name"), "type": "text",
                      "status": f.get("status", "已完成")})
    for d in _list_table_files(uid):
        files.append({"name": d.get("name"), "type": "table",
                      "table_name": d.get("table_name"), "status": "已完成"})
    return _json.dumps(files, ensure_ascii=False)


@tool(description="对指定的文本类文件（PDF/Word/TXT/MD）生成结构化报告（摘要+关键要点+问答）。file_path 为文件路径，question 为可选问题。返回 Markdown 字符串。")
def document_report(file_path: str, question: str = "") -> str:
    from agents.document_report_agent import DocumentReportAgent
    agent = DocumentReportAgent()
    result = agent.run(file_path, question=question or None)
    return result["markdown"]
```

- [ ] **Step 4: Register tools in ReactAgent**

`agent/agent/react_agent.py` import 行（17-20 与 25-28）的 `agent_tools` import 元组末尾加 `list_user_files, document_report`；`tools=[...]` 列表（第 37-40 行）末尾加这两个。

- [ ] **Step 5: Run test**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_list_user_files_tool.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd C:/Users/86131/Multi-Agent-Data-Analysis-System
git add agent/agent/tools/agent_tools.py agent/agent/react_agent.py tests/test_list_user_files_tool.py
git commit -m "feat(s6): add list_user_files and document_report tools to ReactAgent"
```

---

## Task 7: 文件管理前端 + 大文件/解析状态 + 错误兜底

**Files:**
- Modify: `agent/api/fastapi_server.py`（`HTML_TEMPLATE` 侧边栏：统一文件管理面板；上传进度；解析状态轮询；失败提示）
- Modify: `agent/api/fastapi_server.py` 现有 `/api/knowledge/upload` 与 `/api/datasets/upload`：补解析状态字段、大文件阈值校验文案。

**目标：** 前端把现有「知识库」与「数据集」面板合并/对照为统一「文件管理」视图，展示两类文件 + 状态 + 删除；上传时浏览器原生进度 + 上传后轮询 `GET /api/files` 刷新状态；失败项显示原因 + 重试。大文件（PDF/Excel >50MB）上传前给预估或拒绝文案。

- [ ] **Step 1: 读现有两个上传端点与前端面板**

Read `fastapi_server.py` 的 `/api/knowledge/upload`（约 1900 行）与 `/api/datasets/upload`（约 1604 行），以及 `HTML_TEMPLATE` 中 `kb-section` 与 `ds-section` 的结构。

- [ ] **Step 2: 后端补状态字段与阈值**

在两个上传端点中：上传前 `if size > 50*1024*1024` 对 PDF/Excel 返回 413 + 文案「文件过大（XXMB），建议拆分或压缩后再上传」；解析异常 `except` 分支把文件元数据状态置「失败」+ 原因存库（datasources_db 已有可扩展列则用之，文本类在 md5/列表里记失败标记）。

- [ ] **Step 3: 前端统一文件管理面板**

在 `HTML_TEMPLATE` 侧边栏新增 `#files-section`，调 `GET /api/files` 渲染列表（每项显示 name / type 图标 / size / 时间 / 状态徽章 / 删除按钮）；拖拽上传区按扩展名分流到 `/api/knowledge/upload` 或 `/api/datasets/upload`，用 `XMLHttpRequest.upload.onprogress` 显示百分比；上传完成后 `setInterval` 轮询 `/api/files` 直到该项 status≠处理中（或超时 60s 停止）；失败项展示原因 + 「重试」按钮。删除项按 type 调对应 DELETE 端点。

- [ ] **Step 4: 手动冒烟**

启动服务，上传一个 PDF（文本类）与一个 CSV（表格类），确认两类都出现在统一列表；上传一个大文件确认阈值文案；上传一个损坏文件确认「失败」状态与原因。

- [ ] **Step 5: Commit**

```bash
cd C:/Users/86131/Multi-Agent-Data-Analysis-System
git add agent/api/fastapi_server.py
git commit -m "feat(s7): unified file-manager panel, upload progress, parse-status polling, large-file guard"
```

---

## Task 8: 安全回归 — 用户隔离与 SQL 注入

**Files:**
- Test: `tests/test_user_isolation_files.py`、`tests/test_settings_isolation.py`

**Interfaces:** 验证 Task 1/3/4/6 的用户级隔离与标识符安全。

- [ ] **Step 1: Write isolation tests**

`tests/test_settings_isolation.py`:
```python
import os, sys
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
sys.path.insert(0, PROJECT_ROOT)
import agent.database.user_settings_db as usd_mod

def _fresh(tmp_path):
    usd_mod.DB_PATH = str(tmp_path / "u.db"); usd_mod._ensure_db(); usd_mod._init_db()
    return usd_mod.UserSettingsDB()

def test_user_a_invisible_to_b(tmp_path):
    db = _fresh(tmp_path)
    db.upsert("A", {"llm_api_key": "sk-aaa", "llm_model_name": "qwen-a"})
    assert db.get("B") is None
    assert db.has("B") is False
```

`tests/test_user_isolation_files.py`：用 `monkeypatch` 切换 `_current_user_id` 在 `list_user_files`，断言 A 调用只返回 A 的文件（用桩 `_list_text_files`/`_list_table_files` 按 user_id 过滤）。

- [ ] **Step 2: Run**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd agent && python -m pytest tests/test_settings_isolation.py tests/test_user_isolation_files.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
cd C:/Users/86131/Multi-Agent-Data-Analysis-System
git add tests/test_settings_isolation.py tests/test_user_isolation_files.py
git commit -m "test(s8): user isolation for settings and files; SQL idents already covered by sqlglot sandbox"
```

---

## Self-Review（已执行）

1. **Spec 覆盖**：①配置管理→Task 1,2,3；②文件管理→Task 4,7（上传/删除/列表/状态/进度/大文件）；③报告图表→Task 5（文本报告）+ Task 6（工具与 LLM 路由）+ 复用 VisualizationAgent/ReportAgent/ExportAgent（表格管道已存在）；非功能→Task 7,8。无遗漏。
2. **占位符**：无 TBD/TODO；Task 3/7 的前端 HTML/JS 因体量大以「遵循现有 ds-section 结构」指明，但给出了明确字段、端点、轮询超时、阈值数值，非空泛。
3. **类型一致**：`UserSettingsDB.get/upsert/has/get_masked`、`get_chat_model/get_embed_model/reload_model_config`、`DocumentReportAgent.run(file_path, question)->{markdown,file}`、`list_user_files()->str`、`document_report(file_path, question)->str` 在各 Task 间签名一致。
4. **风险注记**：Task 4 依赖 `VectorStoreService.list_files(user_id)` 与 `DatasourcesDB.list_datasets(user_id)` 的现有签名；若不存在或不带 user_id 过滤，需在本 Task 内补齐——计划已注明先读源码确认。
