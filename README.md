# InsightForge AI — 多智能体协作数据分析平台

> 基于 **LangChain 1.3 + LangGraph 1.2** 的多智能体数据分析平台。用户用自然语言提问，系统在「智能客服」与「数据分析」两种模式间由 LLM 自主切换，编排 SQL 查询、趋势 / 产品 / 风险分析、交互式图表与多格式报告导出的完整链路。本文档的所有结论均来自源码核验（附 `文件:行号`），而非沿用旧版描述。

---

## 目录

- [项目亮点](#项目亮点)
- [系统架构总览](#系统架构总览)
- [核心设计深度剖析](#核心设计深度剖析)
  - [1. 子代理编排：静态 _agent_map 与顺序派发](#1-子代理编排静态-_agent_map-与顺序派发)
  - [2. NL→SQL 只读沙箱（AST 级，非进程隔离）](#2-nlsql-只读沙箱ast-级非进程隔离)
  - [3. 多用户隔离模型（contextvars + owner_user_id）](#3-多用户隔离模型contextvars--owner_user_id)
  - [4. 记忆系统：两层结构与当前限制](#4-记忆系统两层结构与当前限制)
  - [5. RAG 两阶段检索](#5-rag-两阶段检索)
  - [6. 模型工厂与配置热重载（单模型，无 fallback 链）](#6-模型工厂与配置热重载单模型无-fallback-链)
  - [7. SSE 流式与跨线程进度推送](#7-sse-流式与跨线程进度推送)
  - [8. 文件分轨：文本入向量库 / 表格入 DuckDB](#8-文件分轨文本入向量库--表格入-duckdb)
- [数据分析流水线数据流](#数据分析流水线数据流)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [HTTP API 一览](#http-api-一览)
- [测试](#测试)
- [能力边界与已知限制](#能力边界与已知限制)
- [安全说明](#安全说明)

---

## 项目亮点

- **双模式、LLM 自主路由**：单一 `ReactAgent`（LangGraph `create_agent`）入口承载 15 个 `@tool`，由 LLM 依据工具描述自行判断走 RAG 问答、文档摘要、还是触发 `run_full_analysis` 完整分析链路——不存在硬编码路由器，路由即"模型选工具"。
- **AST 级 NL→SQL 只读沙箱**：用 `sqlglot` 将 LLM 生成的 SQL 解析为 DuckDB 方言 AST（而非关键词扫描），拦截多语句注入、文件读取（`read_csv_auto`）、SSRF（`httpfs`/`glob`）、DDL/DML、扩展加载（`INSTALL`/`LOAD`）与 `PRAGMA`/`CALL`/`SET`。配合 `safe_ident()` 双引号转义，构成"查询通道 vs 管理通道"的强边界。
- **按用户隔离的内存 OLAP**：每个 `user_id` 拥有独立 DuckDB `:memory:` 实例，CSV/Excel 上传后落盘并在实例创建时按 `owner_user_id` 重载；MySQL/PostgreSQL 通过 `postgres_scan`/`mysql_scan` 扩展以视图挂载，支持跨源 JOIN。
- **两阶段 RAG 检索**：ChromaDB 粗召回（k=15）→ DashScope `gte-rerank-v2` 精排（top_n=3，阈值 0.3），rerank 失败时自动降级回粗召回结果，永不返回空。
- **跨线程实时进度推送**：同步 ReAct Agent 跑在后台守护线程，通过 `asyncio.Queue` + `loop.call_soon_threadsafe` 桥接回异步 SSE 循环；`ProgressEmitter` 经 `contextvars` 让 `PlannerAgent` 的步骤事件绕过被阻塞的生成器直达前端，长任务期间 15s 心跳保活。
- **配置即生效，无需重启**：网页「账号设置」保存的 LLM Key / 模型名 / base_url 即时写入并清空该用户的模型缓存与 Agent 实例，下次请求按新配置重建。API Key 以 Fernet 对称加密存于 SQLite，前端掩码回显。
- **离线可跑的测试套件**：14 个测试文件、96 个用例，LLM 与外部服务 100% mock，约 13 秒跑完，无任何网络依赖。

> 诚实声明：本项目**没有**进程 / 容器 / 虚拟化级代码执行沙箱、**没有**多模态（图像 / 音频 / 视频）理解、**没有**模型故障自动 fallback 链、**没有**子代理动态插件加载。详见 [能力边界与已知限制](#能力边界与已知限制)。

---

## 系统架构总览

```
                          浏览器 (index.html 登录页 / app.html 工作台)
                                        │  Bearer Token / Cookie
                                        ▼
                    FastAPI (:8502)  ── require_auth (Depends, 401 拦截)
                          │            ├─ SSE: /api/chat
                          │            ├─ 同步: /api/analysis
                          │            └─ 会话/设置/文件/数据集/知识库 REST
                          │
            ┌─────────────┴──────────────────────────────┐
            ▼                                              ▼
   ReactAgent (智能客服模式)                       PlannerAgent (数据分析模式)
   langchain create_agent                          LLM 生成 JSON 计划 → 顺序派发
   15 个 @tool + 3 个中间件                         _agent_map (7 个子代理)
            │                                              │
            │  run_full_analysis ──────────────────────────┘
            │  document_report ── DocumentReportAgent (独立)
            │  rag_sumarize ── RAG 服务
            ▼
   PlannerAgent 顺序派发 (prev_results 串联):
     SQLAgent → TrendAgent → ProductAgent → RiskAgent
              → VisualizationAgent → ReportAgent → ExportAgent
        │           │           │            │
        └─── analysis/*.py (纯 pandas/numpy，无 LLM) ────┘
```

**两种模式**

- **智能客服模式（ReactAgent）**：`agent/agent/react_agent.py`，基于 LangGraph `create_agent` 构建（非旧版 `AgentExecutor`）。绑定按 `user_id` 缓存的 LLM、15 个工具、3 个中间件（`monitor_tool` / `log_before_model` / `report_prompt_switch`）。`execute_stream` 是同步生成器，由 FastAPI 放进后台线程跑（见 [§7](#7-sse-流式与跨线程进度推送)）。
- **数据分析模式（PlannerAgent）**：`agent/agents/planner_agent.py`，先用 LLM 生成 JSON 执行计划（`_create_plan`），失败时回退到关键词默认计划（`_default_plan`），再按计划顺序调度子代理。

---

## 核心设计深度剖析

### 1. 子代理编排：静态 `_agent_map` 与顺序派发

子代理采用**静态注册**：在 `PlannerAgent.__init__` 中直接实例化 7 个子代理，并统一把它们的 `.model` 指向当前用户的 LLM，使整条流水线共享同一份按 `user_id` 隔离的模型配置（`planner_agent.py:115-129`）。

```python
self._agent_map = {
    "sql_query":        self._run_sql,
    "trend_analysis":   self._run_trend,
    "product_analysis": self._run_product,
    "risk_analysis":    self._run_risk,
    "visualization":    self._run_visualization,
    "report":           self._run_report,
    "export":           self._run_export,
}
```
> `planner_agent.py:133-141`。另有 `DocumentReportAgent` **不在** `_agent_map` 中，仅由 `document_report` 工具按需懒加载调用（`agent_tools.py:357`）。

**派发循环**（`planner_agent.py:237-278`）是单线程顺序 `for step in plan:`，每个 `handler(task, results, ctx)` 同步调用。结果同时写入两个键，供下游按名取用：

```python
results[step_key] = step_result                # "step_N"
results[f"{agent_name}_result"] = step_result  # 如 "sql_query_result"
```

**依赖与容错语义**（关键，需准确理解）：

- `depends_on` 仅做"依赖的 `step_N` 是否存在且非 None"检查；若依赖未就绪，该步直接 `continue` **跳过**，不排队、不等待、不重试（`planner_agent.py:243-251`）。
- 单步抛异常 → 记入 `errors`、该步置 `None`、发出 `step_error` 进度；下游依赖该步的会被跳过（`planner_agent.py:268-278`）。
- **没有并行执行**：即便 `trend` 与 `product` 都只依赖 `sql_query`，也只能串行跑。
- **没有跨代理重试 / fallback**：唯一的重试是 `SQLAgent._fix_sql` 的错误回灌重生成（最多 2 次重试 = 3 次尝试，`sql_agent.py:88-114`）。
- `success = len(errors) == 0`（`planner_agent.py:294`）。

**通信契约**：`SQLAgent` 产出的 `dataframe_json`（records-orient JSON 字符串）是主数据载体，经 `sql_query_result` 键流入 `Trend`/`Product`/`Risk`/`Viz`；`ReportAgent` 聚合全部 `*_result`（`planner_agent.py:477-484`）。

### 2. NL→SQL 只读沙箱（AST 级，非进程隔离）

LLM 生成的 SQL 在执行前必须通过 `_assert_read_only(sql)`（`duckdb_manager.py:93-136`）。它用 `sqlglot.parse(sql, read="duckdb")` 解析为 AST，再做三道校验：

1. **多语句拒绝**：`len(real_stmts) > 1` 即拒（防 `SELECT 1; DROP TABLE x`，`:113-117`）。
2. **语句类型白名单** + **显式黑名单**（双保险）：
   - 白名单 `_READ_ONLY_STMT_TYPES = {select, union, intersect, except, subquery, show, describe, summarize}`（`:39-42`）。
   - 黑名单 `_FORBIDDEN_STMT_TYPES` 含 `create/insert/update/delete/drop/alter/truncate/copy/attach/detach/call/set/pragma/vacuum/merge/replace/install/load`（`:44-48`）。
   - 注释（`:36-37`）说明 `EXPLAIN/LOAD/CALL/VACUUM` 在 DuckDB 方言下回退为 `command` 类型，无法可靠校验，故 `command` 不在白名单——这些语句会被拒绝。
3. **函数级黑名单** `_FORBIDDEN_FUNCTIONS`（`:52-65`）：拦截文件读（`READCSV*`/`READJSON*`/`READPARQUET`/`READBLOB`/`READTEXT*`）、网络（`HTTPFS`/`GLOB*`）、扩展加载（`INSTALL`/`LOAD`）、写盘（`EXPORT*`）、执行（`SYSTEM`/`SHELL`）。`_collect_func_names`（`:76-90`）遍历 AST 函数节点并规范化（去下划线大写），统一覆盖 `read_csv_auto`（Anonymous）与 `ReadCSV`（内置类）两种解析形态。

**标识符与路径**：表名经 `_validate_table_name`（正则 `^[A-Za-z_][A-Za-z0-9_]*$`，`:139-143`）+ `safe_ident`（双引号包裹、内嵌 `"` 双写，`:29-31`）转义；管理通道的 CSV 路径经 `_validate_csv_path` 限定在 `data/` 目录内且禁止单引号（`:146-161`）。

**通道边界**：`DuckDBManager.execute` / `query_df` 走查询通道（过沙箱）；`_load_csv` / `reload_csv` / `load_csv_dataset` / `drop_table` 等管理操作直调 `self.conn.execute` 绕过沙箱（`:97-99`）。

> ⚠️ **重要边界**：这是 **SQL 语句级 AST 校验**，运行在与 FastAPI 同一 Python 进程内，**不是**进程 / 容器 / 虚拟化隔离。它不设 CPU / 内存 / 磁盘 / 行数 / 超时上限——一条只读但昂贵的查询（如递归 CTE、`generate_series(1, 1e12)`、大表自连接）理论上可耗尽内存或长时间运行。生产部署应在前置层叠加资源限制。沙箱测试见 `tests/test_sql_sandbox.py`（31 个用例）。

### 3. 多用户隔离模型（contextvars + owner_user_id）

**请求上下文**用 `contextvars` 透传（`utils/request_context.py`）：`current_user_id` / `current_session_id` 在 `ReactAgent.execute_stream` 的入口处 `set_request_context` 设置、`finally` 中复位（`react_agent.py:54-62`）。各子系统按其取值隔离：

| 子系统 | 隔离键 | 位置 |
|--------|--------|------|
| DuckDB 实例 | `_duckdb_instances[user_id]`（`:memory:` per user） | `duckdb_manager.py:611, 652-680` |
| LLM / Embedding 缓存 | `_chat_model_cache[user_id]` / `_embed_model_cache[user_id]` | `factory.py:37-38, 122-132` |
| 数据集元数据 | 所有 CRUD 按 `owner_user_id` 过滤 `WHERE owner_user_id = ?` | `datasources_db.py` 全方法 |
| 客户档案 | 复合主键 `(customer_id, user_id)`，查询 `WHERE user_id = ?` | `duckdb_manager.py:299, 704-735` |
| 会话 / 对话历史 | `session_id` → `user_id` owner 校验 | `long_term.py:191-201` |

**IDOR 防护**：所有会话端点重新从 token 推导 `user_id`（绝不信任客户端），并与 `_long_term_memory.get_session_owner(session_id)` 比对，不匹配返回 **404（而非 403，防枚举）**——`/api/chat`（`fastapi_server.py:353-356`）、`GET/DELETE/PATCH /api/sessions/{id}`（`:507, :526, :540`）。数据集删除额外校验 realpath 必须在 `_datasets_dir()` 内（`:731-736`）。

> 注意：`_duckdb_instances` 是**无上限的普通 dict**（无 LRU / TTL / 容量上限），高用户 churn 下存在内存增长风险，仅 `close_duckdb(user_id)` 可手动清理。

### 4. 记忆系统：两层结构与当前限制

记忆是**扁平两层**设计，无工作 / 情景 / 语义的进一步分层：

- **短期记忆**（`memory/short_term.py`）：进程内 dict `_session_pool`，每会话保留 `MAX_TURNS = 30` 轮（1 轮 = 1 问 + 1 答）。超过阈值时 `_maybe_compress`（`:54-76`）取最早的 30 轮，调 `ConversationSummarizer`（LLM 摘要，失败回退主题抽取）写入 `self.summary`，再把摘要落盘到长期记忆。
- **长期记忆**（`memory/long_term.py`）：SQLite `database/memory.db`，三张表——`memory_summaries`（滚动摘要）、`chat_sessions`、`conversation_history`（每轮问答）。检索为**纯 SQL 按时间倒序**（`ORDER BY created_at DESC`），无向量、无语义检索。

**注入方式**：`/api/chat` 在追加用户消息前取 `memory.get_context(max_turns=10)`（`fastapi_server.py:343`），作为 `history` 传入 `execute_stream`（`:378`）；`get_context` 在 `summary` 非空时前置一条 `[历史对话摘要]` 系统消息（`short_term.py:40-45`）。

> ⚠️ **当前限制（经核验）**：
> - 长期记忆的滚动摘要 `get_recent_summaries` / `get_user_history` 在聊天链路中**未被调用**——摘要写入了 SQLite 却不回灌 prompt，实际只有进程内 `self.summary` 到达模型。
> - 短期记忆仅按 `user_id` 索引（`short_term.py:95-99`），**不区分 `session_id`**，同一用户切换会话会共享滚动摘要与轮次缓冲。
> - 长期记忆无 TTL / 遗忘机制，`conversation_history` 无限增长。
> - `PlannerAgent` 导入了 `ConversationMemory` 但未使用——分析流水线**不注入任何记忆**。

### 5. RAG 两阶段检索

`RagSummarizerService`（`rag/rag_service.py`）两阶段：

1. **粗召回**：`_coarse_retrieve`（`:57-66`）从 ChromaDB 取 `retrieve_k=15` 条。
2. **精排**：`_rerank`（`:68-120`）调 `dashscope.TextReRank.call(model="gte-rerank-v2", top_n=3)`，过滤 `score < 0.3`。

**降级策略（永不返回空）**：候选数 ≤ top_n 跳过精排；rerank 非 200 / 空结果 / 抛异常 → 回退粗召回前 3；阈值全过滤完 → 仍回退粗召回前 3（`:76-77, :99, :117, :120`）。

> 注意代码层默认值是 403 易错的 `"gte-rerank"`（`rag_service.py:41`），安全完全依赖 `config/rag.yml` 提供 `gte-rerank-v2`；若配置缺失会静默降级（不崩溃，但召回质量下降）。

**向量库**（`rag/vector_store.py`）：ChromaDB 单一全局 collection `agent`，`DashScopeEmbeddings` 嵌入，`RecursiveCharacterTextSplitter`（chunk_size=500 / overlap=50，分隔符含中文 `。`），持久化到 `chroma_db/`，md5 去重，支持增量入库 / 按 source 删除 / 全量重建。**无按用户隔离**——全局共享一个 collection。

**图表知识库**（`rag/chart_knowledge.py`）：SQLite `chart_knowledge.db`，`jieba.cut_for_search`（搜索引擎模式）中文分词后做 `LIKE` 关键词检索。`VisualizationAgent` 每次出图都写入（`visualization_agent.py:114-126`），但分析流水线**不读回**，仅独立的 `get_chart_insights` 工具读取。

### 6. 模型工厂与配置热重载（单模型，无 fallback 链）

`model/factory.py` 提供 `ChatModelFactory` / `EmbeddingsFactory`：

- **Provider 选择**（非故障 fallback）：无 `base_url` 时用 `ChatTongyi`；用户或 `.env` 设了 `llm_base_url` / `LLM_BASE_URL` 时用 `langchain_openai.ChatOpenAI`（`streaming=True`）接入任意 OpenAI 兼容端点（`factory.py:104-108`）。**没有多模型故障切换链**——每个用户单一模型，配置可热替换。
- **按用户缓存**：`_chat_model_cache` / `_embed_model_cache` 以 `user_id`（或 `__default__`）为键，`_config_lock` 保护（`:36-38, 122-132`）。
- **热重载**：`reload_model_config(user_id)` 直接 `pop` 两个缓存条目（`:111-119`），下次 `get_chat_model` 重建。设置保存端点还会调 `_invalidate_user_agents(user_id)`（`fastapi_server.py:209`）丢弃该用户的 Agent 实例。**注意：没有"版本号"机制**——是"保存即清缓存、取用时重建"的失效模式。
- **配置优先级**（`factory.py:65-92`，已核验）：用户网页配置 > `.env` 环境变量 > YAML 默认值。
- **API Key 加密**：在 `database/user_settings_db.py` 而非 factory。`_get_fernet()`（`:25-49`）从 `INSIGHTFORGE_SETTINGS_KEY` 取主密钥；缺失则 `Fernet.generate_key()` 随机生成并追加写入 `.env`（`load_dotenv(override=False)` 先加载避免覆盖既有密钥）。保存时 `f.encrypt`、读取时 `f.decrypt`，前端 `get_masked` 返回 `sk-***456` 形式。

### 7. SSE 流式与跨线程进度推送

`/api/chat` 返回 `StreamingResponse(media_type="text/event-stream")`，每条事件为 `data: <payload>\n\n`，并设 `X-Accel-Buffering: no` 禁用 nginx 缓冲（`fastapi_server.py:438-446`）。

**线程→异步桥** `_stream_with_heartbeat`（`fastapi_server.py:32-79`）：`ReactAgent.execute_stream` 是同步生成器，在 `run_full_analysis` 等长工具期间会阻塞数分钟。为不饿死异步循环，它被放进**守护线程**跑，每个 chunk 经 `loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))` 推入无界 `asyncio.Queue`；主协程 `await asyncio.wait_for(queue.get(), timeout=15)`，超时则 `yield` 一个心跳保活。线程异常装入 `error_box` 在 `finally` 抛回主协程 → `[ERROR]`。

**跨线程进度**：`ProgressEmitter.bind(loop, queue)`（`:48`）共享同一队列；`PlannerAgent` 经 `contextvars` 取到对应 emitter（`planner_agent.py:183`），`emit` 用 `loop.call_soon_threadsafe` 把步骤事件推入队列，从而绕过被阻塞的生成器直达前端。

**SSE 协议 token**：

| Token | 产出位置 | 含义 |
|-------|----------|------|
| `[SESSION]{id}` | `:372` | 当前轮会话 ID |
| `[SESSIONS_RELOAD]` | `:374` | 新建会话时通知前端刷新列表 |
| `[KEEPALIVE]` | `:381` | 15s 心跳，前端仅重置空闲计时 |
| `[STEP:{json}]` | `:391` | 步骤进度（plan / step_start / step_done / step_error / status） |
| `[THINKING]{text}` | `:400`（源自 `react_agent.py:130`） | 工具调用提示，不计入持久化内容 |
| `[CHART:{url}]` | `:421` | 检测到新生成的图表 HTML |
| `[DONE]` | `:433` | 流结束 |
| `[ERROR] {msg}` | `:436` | 流式异常 |

> `[CONTEXT]` 与 `[AUDIT:]` 在前端 `app.js:506, 508-520` 有解析分支，但**后端无任何产出方**——为预留的 dormant 分支。审计通道目前未启用。

> ⚠️ **无服务端取消**：客户端 `AbortController` 中断只取消 `StreamingResponse` 生成器，后台守护线程仍会把 `execute_stream` 跑完（LLM / Agent 工作不被打断），属资源浪费点。

### 8. 文件分轨：文本入向量库 / 表格入 DuckDB

上传按扩展名自动分轨，前端 `_routeFileByExt`（`app.js:1116-1120`）决定路由：

- **表格类**（`csv` / `xlsx` / `xls`）→ `POST /api/datasets/upload` → DuckDB 建表 + `datasources.db` 记元数据（`owner_user_id` 隔离），可直接 SQL / 跨表 JOIN。
- **文本类**（`txt` / `pdf` / `docx` / `md`）→ `POST /api/knowledge/upload` → `VectorStoreService.load_single_document` 增量入 ChromaDB。

`GET /api/files`（`fastapi_server.py:979-1028`）合并两类返回统一列表；删除按 `type` 在前端分流到 `DELETE /api/datasets/{name}`（drop 表 + 删文件 + 删元数据）或 `DELETE /api/knowledge/files/{filename}`（删向量分片 + 删文件）。文件解析：PDF 用 `PyPDFLoader`（仅文本，无 OCR）、DOCX 用 `python-docx`（段落 + 表格按 `|` 拼接）、TXT/MD 用 `TextLoader`（`utils/file_handler.py`）。

---

## 数据分析流水线数据流

以"分析近半年利润下降原因并生成报告"为例，`PlannerAgent` 顺序派发：

```
1. SQLAgent          NL→SQL → DuckDB(沙箱) → dataframe_json  ──┐
   (_fix_sql: 错误回灌重生成, 最多 3 次尝试)                     │
2. TrendAgent        读 sql_query_result → TrendAnalysis       │
                      (月度收入/MoM 增长率/IQR 异常/移动平均)    │
3. ProductAgent      读 sql_query_result → ProductAnalysis     │ prev_results
                      (TOP 产品/类别贡献/高量低利)               │ 串联
4. RiskAgent         读 sql_query_result → AnomalyDetection    │
                      (IQR + Z-score/区域异常/品类亏损)          │
5. VisualizationAgent 读 sql_query_result + trend/product →    │
                      LLM 选图型(失败回退启发式) → Plotly HTML  ─┘
                      每图写入 chart_knowledge SQLite
6. ReportAgent       聚合全部 *_result → Jinja2 渲染 Markdown
                      (2 次 LLM: 执行摘要 + 结论)
7. ExportAgent       Markdown → 导出 (流水线仅产 md+html)
```

> `analysis/*.py` 三模块是**纯 pandas/numpy**（零 LLM 调用）；LLM 洞察生成发生在各 Agent 包装层，失败时回退到纯计算结果。注意 `trend_analysis.py` 只实现了**环比 MoM**（`pct_change`），未实现同比 YoY。

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| AI 框架 | LangChain 1.3 / LangGraph 1.2 | `create_agent` + 中间件 |
| LLM | 通义千问（`ChatTongyi`）/ OpenAI 兼容（`ChatOpenAI`） | 单模型，按 base_url 选 provider |
| 向量 | ChromaDB 1.5 + DashScope Embeddings | 全局 collection `agent` |
| Rerank | DashScope `gte-rerank-v2` | `gte-rerank` 对多数 Key 返回 403，须用 v2 |
| 中文分词 | jieba（搜索引擎模式） | 图表知识库 LIKE 检索 |
| OLAP | DuckDB 1.5 | 按用户 `:memory:` 实例 + `postgres_scan`/`mysql_scan` |
| SQL 安全 | sqlglot 30.12 | AST 只读沙箱 |
| 分析 | pandas / numpy | 趋势 / 产品 / 异常检测算法 |
| 可视化 | Plotly 6.9 | 5 类图表，独立 HTML |
| Web | FastAPI 0.139 + uvicorn | SSE 流式，端口 8502 |
| 报告导出 | python-docx / reportlab / Jinja2 | md / docx / pdf / html（流水线产 md+html） |
| 记忆 | 短期（进程内）+ 长期（SQLite `memory.db`） | |
| 数据库 | SQLite ×6 | users / customers / memory / chart_knowledge / datasources / user_settings |
| 安全 | bcrypt / Fernet / secrets.token_hex | 密码哈希 / Key 加密 / 令牌 |
| 语言 | Python 3.10+ | 推荐 conda 环境 `AnalysisAgent` |

---

## 项目结构

```
Multi-Agent-Data-Analysis-System/
├── README.md                      # 本文件
├── CLAUDE.md                      # 给 AI 助手的项目指引
├── .gitignore                     # 含 .env / agent/.env（已忽略，未跟踪）
└── agent/                         # 所有源码
    ├── .env / .env.example        # 本地密钥（gitignored）/ 模板
    ├── requirements.txt
    ├── api/
    │   ├── fastapi_server.py      # 唯一入口（1232 行）：SSE + REST + 鉴权
    │   ├── auth.py                # require_auth + TTL 令牌缓存
    │   └── static/                # 静态前端：index.html(登录) app.html(工作台)
    │       ├── js/  app.js auth.js icons.js landing.js
    │       └── css/ app.css auth.css landing.css tokens.css
    ├── agent/                     # 嵌套命名空间包（无 __init__.py，双导入兜底）
    │   ├── react_agent.py         # 智能客服入口
    │   └── tools/  agent_tools.py(15 @tool)  middleware.py(3 中间件)
    ├── agents/                    # 数据分析流水线
    │   ├── base.py  planner_agent.py  sql_agent.py
    │   ├── trend_agent.py  product_agent.py  risk_agent.py
    │   ├── visualization_agent.py  report_agent.py
    │   ├── document_report_agent.py  export_agent.py
    ├── analysis/                  # 纯算法：trend / product / anomaly_detection
    ├── visualization/ charts.py   # Plotly 图表生成器
    ├── rag/  rag_service.py  vector_store.py  chart_knowledge.py
    ├── memory/  short_term.py  long_term.py  summarizer.py
    ├── model/  factory.py         # 按用户 LLM/Embedding 缓存 + 热重载
    ├── database/                  # duckdb_manager / user_db / user_settings_db
    │   │                          #   datasources_db / data_resolver / schema_loader
    │   └── *.db                   # 6 个 SQLite 运行时文件
    ├── utils/  config_handler  logger_handler  path_tool  prompt_loader
    │           file_handler  request_context  report_exporter  progress_emitter
    ├── config/  rag.yml  chroma.yml  agent.yml  datasources.yml  prompts.yml
    ├── prompts/  main_prompt  report_prompt  document_report  rag_summarize  (.txt)
    ├── templates/  report_template.md   # Jinja2 报告模板
    ├── data/  datasets/  external/  (上传 / 外部数据落盘)
    ├── chroma_db/                 # 向量库持久化
    ├── reports/  charts/          # 生成的报告与图表（挂载到 /reports）
    ├── logs/                      # 按日 .log（无轮转）
    └── tests/                     # 14 个测试文件
```

> 说明：`agent/agent/` 是两层嵌套的命名空间包（`agent/` 与 `agent/agent/` 均无 `__init__.py`），靠命名空间包机制 + 全仓 `try: from agent.x / except: from x` 双导入模式解析，已通过测试验证可运行。

---

## 快速开始

### 1. 环境要求

- Python 3.10+，推荐 conda 隔离依赖
- 一个 DashScope（通义千问）API Key — [获取](https://dashscope.console.aliyun.com/)

### 2. 安装

```bash
git clone <repository-url>
cd Multi-Agent-Data-Analysis-System/agent

conda create -n AnalysisAgent python=3.10
conda activate AnalysisAgent
pip install -r requirements.txt
```

### 3. 配置 API Key

```bash
cp .env.example .env
```

`.env` 关键字段（**不会提交到 Git**，已 gitignored）：

```dotenv
DASHSCOPE_API_KEY=sk-your-dashscope-api-key
CHAT_MODEL_NAME=qwen3-max            # 默认模型名（.env 优先于 YAML）
EMBEDDING_MODEL_NAME=text-embedding-v4
# INSIGHTFORGE_SETTINGS_KEY=          # 用户配置加密主密钥；缺失时首启自动生成并写回 .env
```

也可启动后在网页「账号设置」面板填写并保存——**保存即时生效，无需重启**，且网页配置优先级高于 `.env`，API Key 以 Fernet 加密存本地、掩码回显。

### 4. 启动服务

```bash
conda activate AnalysisAgent
cd agent
python -m api.fastapi_server
# 访问 http://localhost:8502  →  注册 → 登录 → 开始分析
```

服务监听 `0.0.0.0:8502`，无 `reload`、无 lifespan / startup 钩子；向量库、DuckDB 实例、Agent 均在首次请求时懒加载。

### 5. 跑通第一次分析

1. 登录进入主界面，侧边栏可见「文件管理」「账号设置」。
2. 上传一份 CSV（如销售明细）→ 表格类自动入 DuckDB，轮询到「已就绪」即可分析。
3. 自然语言提问：
   - `分析各月销售趋势并生成报告` → SQL→趋势→可视化→报告→导出全链路
   - `哪个产品类别利润最高？` → SQLAgent 直接答
   - 上传一份 PDF 后问 `总结这份文档要点` → 走 `document_report` 文本报告
4. Plotly 图表内嵌在对话流中（iframe）；结论区可导出 Markdown / HTML。

### 6. 运行测试

```bash
conda activate AnalysisAgent
cd agent
python -m pytest tests/ -v          # 推荐：96 用例，约 13s，全离线
```

> 注意：`python -m unittest discover tests` 只能收集 4 个 `unittest.TestCase` 文件，会漏掉 10 个 pytest 函数式测试文件——**请用 pytest**。

---

## 配置说明

| 文件 | 作用 | 真实默认值 |
|------|------|------------|
| `.env` | **真相源**：`DASHSCOPE_API_KEY` / `CHAT_MODEL_NAME` / `EMBEDDING_MODEL_NAME` / `INSIGHTFORGE_SETTINGS_KEY`（gitignored） | — |
| `config/rag.yml` | rerank 与检索参数（模型名仅作 fallback） | `rerank_model: gte-rerank-v2`、`retrieve_k: 15`、`rerank_top_n: 3`、`rerank_score_threshold: 0.3` |
| `config/chroma.yml` | 向量库与分块 | `collection_name: agent`、`chunk_size: 500`、`chunk_overlap: 50`、`allowed_knowledge_file_type: [txt,pdf,docx,md]` |
| `config/agent.yml` | 外部数据路径 | `external_data_path: data/external/records.csv` |
| `config/datasources.yml` | 管理员预置数据库连接 | `databases: []`（默认空；每条含 name/type/host/port/database/user/`password_env`/tables） |
| `config/prompts.yml` | Prompt 模板路径 | main / rag_summarize / report / document_report 四个 .txt 路径 |

**配置优先级**：用户网页配置 > `.env` > YAML（`factory.py:65-92`）。

`config/datasources.yml` 结构示例：

```yaml
databases:
  - name: local_mysql
    type: mysql              # 或 postgres
    host: 127.0.0.1
    port: 3306
    database: my_business
    user: root
    password_env: MYSQL_PASSWORD   # 引用 .env 变量名，密码不硬编码
    tables: []                     # [] = 自动发现并暴露全部；列表 = 限定
```

> 改动 `chunk_size` 后需清库重灌：前端「📚 知识库 → ⟳ 全量重建索引」或 `POST /api/knowledge/reindex`（需 `confirm=true`）。

---

## HTTP API 一览

| 接口 | 方法 | 鉴权 | 说明 |
|------|------|------|------|
| `/` | GET | — | 登录页 |
| `/app` | GET | cookie/token | 主应用页（未登录 302→`/`） |
| `/api/register` | POST | — | 注册并自动登录、种 cookie |
| `/api/login` | POST | — | 登录，返回 token + 种 cookie |
| `/api/logout` | POST | — | 删 SQLite 会话 + 清令牌缓存 + 清 cookie |
| `/api/me` `/api/profile` | GET/POST | ✓ | 用户信息 / 改昵称（清缓存） |
| `/api/password` | POST | ✓ | 改密码（清缓存） |
| `/api/chat` | POST | ✓ | SSE 流式聊天，按 user_id 隔离 |
| `/api/analysis` | POST | ✓ | 同步 JSON 数据分析 |
| `/api/conversation/history` | GET | ✓ | 长期记忆最近 N 轮 |
| `/api/sessions` `/{id}` | GET/DEL/PATCH | ✓ | 会话列表 / 详情 / 删除 / 重命名（均 IDOR owner 校验，404 防枚举） |
| `/api/settings` `/status` | GET/POST | ✓ | 配置读取（掩码）/ 保存（热重载）/ 是否已配置 |
| `/api/files` | GET | ✓ | 统一文件列表（文本 + 表格） |
| `/api/datasets` `/{name}` `/schema` | GET/POST/DEL | ✓ | 数据集列表 / 上传 / 删除 / schema（DESCRIBE+SUMMARIZE+样本） |
| `/api/datasets/upload` | POST | ✓ | CSV/Excel 上传（multipart，max 100MB） |
| `/api/datasources/reload` | POST | ✓ | 热重载 `datasources.yml` → 挂载外部库 |
| `/api/knowledge/files` `/{name}` | GET/DEL | ✓ | 知识库文件列表（md5/入库态）/ 删除（删向量+文件） |
| `/api/knowledge/upload` | POST | ✓ | 文本文件增量入库（txt/pdf/docx/md） |
| `/api/knowledge/reindex` | POST | ✓ | 全量重建（需 `confirm=true`） |
| `/api/knowledge/stats` | GET | ✓ | 知识库统计 |
| `/api/health` | GET | — | 健康检查 |

> 鉴权为自研 opaque token（`secrets.token_hex(16)`，24h 过期，SQLite 持久化 + 30s 进程内 TTL 缓存），非 JWT/OAuth。`require_auth` 优先取 `Authorization: Bearer`，缺失时回退 `token` cookie（页面导航场景）。

---

## 测试

- **规模**：14 个文件、96 个用例，全量通过、全离线（约 13s）。
- **mock 策略**：LLM 与外部服务 100% mock；`test_sql_sandbox.py` 纯函数无 mock；涉及 DB 的用 temp SQLite / 临时 DuckDB。
- **覆盖重点**：SQL 沙箱 AST 守卫（31 例）、鉴权与重定向循环修复、多用户隔离（数据集 / 设置 / 文件 / 客户档案）、配置优先级、模型缓存热重载、RAG 格式化、文档报告截断。
- **运行器**：pytest（`unittest discover` 会漏 10 个函数式文件，见上）。

```bash
python -m pytest tests/ -v
```

---

## 能力边界与已知限制

为避免误用，以下为经源码核验的能力边界（非缺陷清单，而是"它是什么 / 不是什么"）：

**架构层面**

- 子代理为**静态注册**（`_agent_map` 硬编码），无动态插件 / 注册表 / 磁盘加载机制；新增代理需改 `planner_agent.py` + `agents/__init__.py`。
- 流水线**严格顺序执行**，无并行；`depends_on` 仅用于"跳过未就绪步骤"，不调度并发。
- 无跨代理重试 / fallback；仅 `SQLAgent._fix_sql` 内部错误回灌重生成（最多 3 次）。

**安全层面**

- "沙箱"是 **SQL 语句级 AST 校验**，非进程 / 容器 / 虚拟化隔离；**无 CPU / 内存 / 磁盘 / 行数 / 超时上限**，只读但昂贵的查询存在 DoS 风险。
- 向量库与图表知识库为**全局共享**，无按用户隔离（任意用户的上传知识可被他人检索到）。
- 无 CSRF token、无限流 / 登录防爆破、无 CORS 配置（同源）、无 CSP；图表 iframe 无 `sandbox` 属性；前端 XSS 防护为自研 `escapeHtml` + URL 协议白名单（未用 DOMPurify）。
- 客户端中断 SSE 后，**后台线程仍跑完整任务**，无服务端取消。

**功能层面**

- **无多模态**：仅文本理解，PDF 不做 OCR / 图像解析。
- **无模型 fallback 链**：单模型 per user，`ChatTongyi`/`ChatOpenAI` 为 provider 选择而非故障切换。
- 导出 4 种格式均已实现，但**流水线仅产出 md + html**（`planner_agent.py:498` 硬编码 `formats=["md","html"]`）；docx/pdf 需显式调用且** PDF 会丢弃图片与表格**。
- `DocumentReportAgent` **不走 RAG**，直接把原文截断至 8000 字塞给 LLM。
- 长期记忆摘要**写入但不回灌**聊天 prompt（见 [§4](#4-记忆系统两层结构与当前限制)）；短期记忆按 `user_id` 而非 `session_id` 索引，存在跨会话串扰。
- `get_weather` / `get_user_id` / `get_user_location` 三个工具为**演示桩**（返回硬编码 / 随机值）。
- 审计通道 `[AUDIT]` / `[CONTEXT]` 仅有前端解析、无后端产出，处于 dormant 状态。
- `_duckdb_instances` 为无上限 dict，高用户 churn 下内存只增不减。
- 日志按日落盘但**无轮转 / 容量上限**。

---

## 安全说明

- `.env` 已在 `.gitignore` 中忽略且**未被 git 跟踪**（`.gitignore:2-3`），内含真实 `DASHSCOPE_API_KEY` 与 `INSIGHTFORGE_SETTINGS_KEY`。请勿提交；若历史上曾误提交，应立即在 DashScope 控制台轮换 Key。
- 用户密码以 `bcrypt` 哈希存储（兼容旧 SHA-256 并在下次登录惰性升级）；令牌为 `secrets.token_hex(16)` 随机串、24h 过期、登出 / 改密 / 改昵称即清进程内缓存。
- 用户 API Key 以 Fernet 对称加密存于 `user_settings.db`，主密钥在 `INSIGHTFORGE_SETTINGS_KEY`；任何能读 `.env` 者可解密，请妥善保护该文件与运行环境。
- 会话端点均做 owner 校验并返回 404 防枚举；数据集 / 文件操作按 `owner_user_id` 隔离并有路径穿越防护。
- 生产部署建议：在反代层叠加超时 / 限流 / 请求体大小上限，并为只读 SQL 查询增加资源配额（补齐沙箱未覆盖的 DoS 面）。

---

*本项目由 InsightForge AI 多智能体协作数据分析系统驱动。文档基于源码核验，如与代码不一致以代码为准。*


---

## 版本更新记录

### v0.2（2026-07-23）

> 相对 v0.1 的主要更新。结论基于源码与提交记录核验。

**界面与交互**
- 欢迎页重塑：hero + 功能卡片 + 登录/注册模态框（remember-me），统一 sci-tech 科技风设计语言。
- 主工作台 sci-tech 主题重塑（信息架构保留）；移除用户头像功能；新增 SVG 图标库 `agent/api/static/js/icons.js`。
- 前端从 `fastapi_server.py` 内联 HTML 抽离至 `agent/api/static/`（静态化 + no-cache 中间件 + 版本号破缓存）。

**鉴权**
- 全站 `require_auth` 依赖 + TTL 令牌 LRU 缓存；cookie + token 登录态。
- 登出 / 改密 / 改昵称即清进程内令牌缓存；修复 landing 页 401 重定向循环。
- 用户档案 / 改密端点；`bcrypt` 密码哈希（兼容旧 SHA-256 惰性升级）。

**能力增强**
- 跨线程步骤进度：`ProgressEmitter` + 前端 `[STEP]` / `[KEEPALIVE]` 步骤清单。
- 图表与可视化：`visualization_agent` + `charts` 混合样式增强。
- 配置热重载：`reload_model_config(user_id)` 失效缓存并丢弃 Agent 实例。
- 统一文件管理面板：上传进度、解析状态轮询、大文件提示（50MB 预估 / 100MB 上限）。
- `DocumentReportAgent`：文本文件摘要 / 要点 / 问答报告。

**Bug 修复**
- 知识库“已入库但读不到”：根因为 `md5.text` 与 chroma 实际状态偏离（如 chroma_db 被删而 md5 残留），文件永久卡死。修复后以 chroma 实际分片为“可读”真相，`_ingest_if_needed` 在偏离时自愈重灌，列表 `ingested` 状态不再仅凭 md5。

**工程**
- 测试套件扩充至 101 passed（新增知识库自愈、鉴权、配置优先级、工厂等用例）。
- 移除废弃 `app.py` / SDD 文档；新增 `scripts/repo_cleanup.sh`。
- `.gitignore` 补齐知识库上传文件（pdf/docx）。

### v0.1

初始可用版本：多智能体协作数据分析平台（智能客服 + 数据分析双模式）、NL->SQL 只读沙箱、多用户隔离、RAG 两阶段检索、DuckDB 多源、多格式报告导出。
