# 核心设计深度剖析

## 1. 子代理编排：静态 `_agent_map` 与顺序派发

子代理采用**静态注册**：在 `PlannerAgent.__init__` 中直接实例化 7 个子代理，并统一把它们的 `.model` 指向当前用户的 LLM，使整条流水线共享同一份按 `user_id` 隔离的模型配置（`planner_agent.py:116-130`）。

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
> `planner_agent.py:134-141`。另有 `DocumentReportAgent` **不在** `_agent_map` 中，仅由 `document_report` 工具按需懒加载调用（`agent_tools.py:377`）。

**派发循环**（`planner_agent.py:243-301`）是单线程顺序 `for step in plan:`，每个 `handler(task, results, ctx)` 同步调用。结果同时写入两个键，供下游按名取用：

```python
results[step_key] = step_result                # "step_N"
results[f"{agent_name}_result"] = step_result  # 如 "sql_query_result"
```

**依赖与容错语义**（关键，需准确理解）：

- `depends_on` 仅做"依赖的 `step_N` 是否存在且非 None"检查；若依赖未就绪，该步直接 `continue` **跳过**，不排队、不等待、不重试（`planner_agent.py:248-257`）。
- 单步抛异常 -> 记入 `errors`、该步置 `None`、发出 `step_error` 进度；下游依赖该步的会被跳过（`planner_agent.py:274-278`）。
- **没有并行执行**：即便 `trend` 与 `product` 都只依赖 `sql_query`，也只能串行跑。
- **没有跨代理重试 / fallback**：唯一的重试是 `SQLAgent._fix_sql` 的错误回灌重生成（最多 2 次重试 = 3 次尝试，`sql_agent.py:88-114`）。
- `success = len(errors) == 0`（`planner_agent.py:300`）。

**通信契约**：`SQLAgent` 产出的 `dataframe_json`（records-orient JSON 字符串）是主数据载体，经 `sql_query_result` 键流入 `Trend`/`Product`/`Risk`/`Viz`；`ReportAgent` 聚合全部 `*_result`（`planner_agent.py:502-510`）。

## 2. NL->SQL 只读沙箱（AST 级，非进程隔离）

LLM 生成的 SQL 在执行前必须通过 `_assert_read_only(sql)`（`duckdb_manager.py:93-136`）。它用 `sqlglot.parse(sql, read="duckdb")` 解析为 AST，再做三道校验：

1. **多语句拒绝**：`len(real_stmts) > 1` 即拒（防 `SELECT 1; DROP TABLE x`，`:113-117`）。
2. **语句类型白名单** + **显式黑名单**（双保险）：
   - 白名单 `_READ_ONLY_STMT_TYPES = {select, union, intersect, except, subquery, show, describe, summarize}`（`:39-42`）。
   - 黑名单 `_FORBIDDEN_STMT_TYPES` 含 `create/insert/update/delete/drop/alter/truncate/copy/attach/detach/call/set/pragma/vacuum/merge/replace/install/load`（`:44-48`）。
   - 注释（`:36-37`）说明 `EXPLAIN/LOAD/CALL/VACUUM` 在 DuckDB 方言下回退为 `command` 类型，无法可靠校验，故 `command` 不在白名单--这些语句会被拒绝。
3. **函数级黑名单** `_FORBIDDEN_FUNCTIONS`（`:52-65`）：拦截文件读（`READCSV*`/`READJSON*`/`READPARQUET`/`READBLOB`/`READTEXT*`）、网络（`HTTPFS`/`GLOB*`）、扩展加载（`INSTALL`/`LOAD`）、写盘（`EXPORT*`）、执行（`SYSTEM`/`SHELL`）。`_collect_func_names`（`:76-90`）遍历 AST 函数节点并规范化（去下划线大写），统一覆盖 `read_csv_auto`（Anonymous）与 `ReadCSV`（内置类）两种解析形态。

**标识符与路径**：表名经 `_validate_table_name`（正则 `^[A-Za-z_][A-Za-z0-9_]*$`，`:139-143`）+ `safe_ident`（双引号包裹、内嵌 `"` 双写，`:29-31`）转义；管理通道的 CSV 路径经 `_validate_csv_path` 限定在 `data/` 目录内且禁止单引号（`:146-161`）。

**通道边界**：`DuckDBManager.execute` / `query_df` 走查询通道（过沙箱）；`_load_csv` / `reload_csv` / `load_csv_dataset` / `drop_table` 等管理操作直调 `self.conn.execute` 绕过沙箱（`:97-99`）。

> ⚠️ **重要边界**：这是 **SQL 语句级 AST 校验**，运行在与 FastAPI 同一 Python 进程内，**不是**进程 / 容器 / 虚拟化隔离。它不设 CPU / 内存 / 磁盘 / 行数 / 超时上限--一条只读但昂贵的查询（如递归 CTE、`generate_series(1, 1e12)`、大表自连接）理论上可耗尽内存或长时间运行。生产部署应在前置层叠加资源限制。沙箱测试见 `tests/test_sql_sandbox.py`（31 个用例）。

## 3. 多用户隔离模型（contextvars + owner_user_id）

**请求上下文**用 `contextvars` 透传（`utils/request_context.py`）：`current_user_id` / `current_session_id` 在 `ReactAgent.execute_stream` 的入口处 `set_request_context` 设置、`finally` 中复位（`react_agent.py:54-62`）。各子系统按其取值隔离：

| 子系统 | 隔离键 | 位置 |
|--------|--------|------|
| DuckDB 实例 | `_duckdb_instances[user_id]`（`:memory:` per user） | `duckdb_manager.py:611, 652-680` |
| LLM / Embedding 缓存 | `_chat_model_cache[user_id]` / `_embed_model_cache[user_id]` | `factory.py:37-38, 122-132` |
| 数据集元数据 | 所有 CRUD 按 `owner_user_id` 过滤 `WHERE owner_user_id = ?` | `datasources_db.py` 全方法 |
| 客户档案 | 复合主键 `(customer_id, user_id)`，查询 `WHERE user_id = ?` | `duckdb_manager.py:299, 704-735` |
| 会话 / 对话历史 | `session_id` -> `user_id` owner 校验 | `long_term.py:191-201` |

**IDOR 防护**：所有会话端点重新从 token 推导 `user_id`（绝不信任客户端），并与 `_long_term_memory.get_session_owner(session_id)` 比对，不匹配返回 **404（而非 403，防枚举）**--`/api/chat`（`fastapi_server.py:363`）、`GET/DELETE/PATCH /api/sessions/{id}`（`:516, :535, :549`）。数据集删除额外校验 realpath 必须在 `_datasets_dir()` 内（`:740-741`）。

> 注意：`_duckdb_instances` 是**无上限的普通 dict**（无 LRU / TTL / 容量上限），高用户 churn 下存在内存增长风险，仅 `close_duckdb(user_id)` 可手动清理。

## 4. 记忆系统：两层结构与当前限制

记忆是**扁平两层**设计，无工作 / 情景 / 语义的进一步分层：

- **短期记忆**（`memory/short_term.py`）：进程内 dict `_session_pool`，每会话保留 `MAX_TURNS = 30` 轮（1 轮 = 1 问 + 1 答）。超过阈值时 `_maybe_compress`（`:54-76`）取最早的 30 轮，调 `ConversationSummarizer`（LLM 摘要，失败回退主题抽取）写入 `self.summary`，再把摘要落盘到长期记忆。
- **长期记忆**（`memory/long_term.py`）：SQLite `database/memory.db`，三张表--`memory_summaries`（滚动摘要）、`chat_sessions`、`conversation_history`（每轮问答）。检索为**纯 SQL 按时间倒序**（`ORDER BY created_at DESC`），无向量、无语义检索。

**注入方式**：`/api/chat` 在追加用户消息前取 `memory.get_context(max_turns=10)`（`fastapi_server.py:352`），作为 `history` 传入 `execute_stream`（`:387`）；`get_context` 在 `summary` 非空时前置一条 `[历史对话摘要]` 系统消息（`short_term.py:40-45`）。

> ⚠️ **当前限制（经核验）**：
> - 长期记忆的滚动摘要 `get_recent_summaries` / `get_user_history` 在聊天链路中**未被调用**--摘要写入了 SQLite 却不回灌 prompt，实际只有进程内 `self.summary` 到达模型。
> - 短期记忆仅按 `user_id` 索引（`short_term.py:95-99`），**不区分 `session_id`**，同一用户切换会话会共享滚动摘要与轮次缓冲。
> - 长期记忆无 TTL / 遗忘机制，`conversation_history` 无限增长。
> - 分析桥 `QueryRewriter` 现读取短期记忆 `get_session(user_id).get_context(max_turns=6)`（`planner_agent.py:326`）用于消解指代--这是流水线首次接入记忆；但 `ConversationMemory` 类名仍仅 import 未直接使用，长期记忆摘要依旧不回灌。

## 5. RAG 两阶段检索

`RagSummarizerService`（`rag/rag_service.py`）先经 `RetrievalQueryRewriter` 多查询扩展，再两阶段检索（改写详见 [§9②](#9-query-rewriting两点改写adr-0002)）：

1. **粗召回**：`_coarse_retrieve`（`:62`）从 ChromaDB 取 `retrieve_k=15` 条。
2. **精排**：`_rerank`（`:66`）调 `dashscope.TextReRank.call(model="gte-rerank-v2", top_n=3)`，过滤 `score < 0.3`。

**降级策略（永不返回空）**：候选数 ≤ top_n 跳过精排；rerank 非 200 / 空结果 / 抛异常 -> 回退粗召回前 3；阈值全过滤完 -> 仍回退粗召回前 3（`:74-75, :97, :115, :118`）。

> 注意代码层默认值是 403 易错的 `"gte-rerank"`（`rag_service.py:43`），安全完全依赖 `config/rag.yml` 提供 `gte-rerank-v2`；若配置缺失会静默降级（不崩溃，但召回质量下降）。

**向量库**（`rag/vector_store.py`）：ChromaDB 单一全局 collection `agent`，`DashScopeEmbeddings` 嵌入，`RecursiveCharacterTextSplitter`（chunk_size=500 / overlap=50，分隔符含中文 `。`），持久化到 `chroma_db/`，md5 去重，支持增量入库 / 按 source 删除 / 全量重建。**无按用户隔离**--全局共享一个 collection。

**图表知识库**（`rag/chart_knowledge.py`）：SQLite `chart_knowledge.db`，`jieba.cut_for_search`（搜索引擎模式）中文分词后做 `LIKE` 关键词检索。`VisualizationAgent` 每次出图都写入（`visualization_agent.py:114-126`），但分析流水线**不读回**，仅独立的 `get_chart_insights` 工具读取。

## 6. 模型工厂与配置热重载（单模型，无 fallback 链）

`model/factory.py` 提供 `ChatModelFactory` / `EmbeddingsFactory`：

- **Provider 选择**（非故障 fallback）：无 `base_url` 时用 `ChatTongyi`；用户或 `.env` 设了 `llm_base_url` / `LLM_BASE_URL` 时用 `langchain_openai.ChatOpenAI`（`streaming=True`）接入任意 OpenAI 兼容端点（`factory.py:104-108`）。**没有多模型故障切换链**--每个用户单一模型，配置可热替换。
- **按用户缓存**：`_chat_model_cache` / `_embed_model_cache` 以 `user_id`（或 `__default__`）为键，`_config_lock` 保护（`:36-38, 122-132`）。
- **热重载**：`reload_model_config(user_id)` 直接 `pop` 两个缓存条目（`:111-119`），下次 `get_chat_model` 重建。设置保存端点还会调 `_invalidate_user_agents(user_id)`（`fastapi_server.py:213`）丢弃该用户的 Agent 实例。**注意：没有"版本号"机制**--是"保存即清缓存、取用时重建"的失效模式。
- **配置优先级**（`factory.py:65-92`，已核验）：用户网页配置 > `.env` 环境变量 > YAML 默认值。
- **API Key 加密**：在 `database/user_settings_db.py` 而非 factory。`_get_fernet()`（`:25-49`）从 `INSIGHTFORGE_SETTINGS_KEY` 取主密钥；缺失则 `Fernet.generate_key()` 随机生成并追加写入 `.env`（`load_dotenv(override=False)` 先加载避免覆盖既有密钥）。保存时 `f.encrypt`、读取时 `f.decrypt`，前端 `get_masked` 返回 `sk-***456` 形式。

## 7. SSE 流式与跨线程进度推送

`/api/chat` 返回 `StreamingResponse(media_type="text/event-stream")`，每条事件为 `data: <payload>\n\n`，并设 `X-Accel-Buffering: no` 禁用 nginx 缓冲（`fastapi_server.py:447-453`）。

**线程->异步桥** `_stream_with_heartbeat`（`fastapi_server.py:32-79`）：`ReactAgent.execute_stream` 是同步生成器，在 `run_full_analysis` 等长工具期间会阻塞数分钟。为不饿死异步循环，它被放进**守护线程**跑，每个 chunk 经 `loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))` 推入无界 `asyncio.Queue`；主协程 `await asyncio.wait_for(queue.get(), timeout=15)`，超时则 `yield` 一个心跳保活。线程异常装入 `error_box` 在 `finally` 抛回主协程 -> `[ERROR]`。

**跨线程进度**：`ProgressEmitter.bind(loop, queue)`（`:48`）共享同一队列；`PlannerAgent` 经 `contextvars` 取到对应 emitter（`planner_agent.py:184`），`emit` 用 `loop.call_soon_threadsafe` 把步骤事件推入队列，从而绕过被阻塞的生成器直达前端。

**SSE 协议 token**：

| Token | 产出位置 | 含义 |
|-------|----------|------|
| `[SESSION]{id}` | `:381` | 当前轮会话 ID |
| `[SESSIONS_RELOAD]` | `:383` | 新建会话时通知前端刷新列表 |
| `[KEEPALIVE]` | `:390` | 15s 心跳，前端仅重置空闲计时 |
| `[STEP:{json}]` | `:400` | 步骤进度（plan / step_start / step_done / step_error / status） |
| `[THINKING]{text}` | `:409`（源自 `react_agent.py:130`） | 工具调用提示，不计入持久化内容 |
| `[CHART:{url}]` | `:430` | 检测到新生成的图表 HTML |
| `[DONE]` | `:442` | 流结束 |
| `[ERROR] {msg}` | `:445` | 流式异常 |

> `[CONTEXT]` 与 `[AUDIT:]` 在前端 `app.js:506, 508-520` 有解析分支，但**后端无任何产出方**--为预留的 dormant 分支。审计通道目前未启用。

> ⚠️ **无服务端取消**：客户端 `AbortController` 中断只取消 `StreamingResponse` 生成器，后台守护线程仍会把 `execute_stream` 跑完（LLM / Agent 工作不被打断），属资源浪费点。

## 8. 文件分轨：文本入向量库 / 表格入 DuckDB

上传按扩展名自动分轨，前端 `_routeFileByExt`（`app.js:1116-1120`）决定路由：

- **表格类**（`csv` / `xlsx` / `xls`）-> `POST /api/datasets/upload` -> DuckDB 建表 + `datasources.db` 记元数据（`owner_user_id` 隔离），可直接 SQL / 跨表 JOIN。
- **文本类**（`txt` / `pdf` / `docx` / `md`）-> `POST /api/knowledge/upload` -> `VectorStoreService.load_single_document` 增量入 ChromaDB。

`GET /api/files`（`fastapi_server.py:995`）合并两类返回统一列表；删除按 `type` 在前端分流到 `DELETE /api/datasets/{name}`（drop 表 + 删文件 + 删元数据）或 `DELETE /api/knowledge/files/{filename}`（删向量分片 + 删文件）。文件解析：PDF 用 `PyPDFLoader`（仅文本，无 OCR）、DOCX 用 `python-docx`（段落 + 表格按 `|` 拼接）、TXT/MD 用 `TextLoader`（`utils/file_handler.py`）。

## 9. Query Rewriting（两点改写，ADR-0002）

系统原本把原始 query 直接送入分析流水线与 RAG 检索，不做变换。现新增两个**独立改写组件**（均非 `BaseAgent` 子类），因两处需要不同变换（[ADR-0002](adr/0002-query-rewriting-two-points.md)）：

**① 分析桥 `QueryRewriter`**（`agents/query_rewriter.py`，在 `PlannerAgent.run` 的 `_create_plan` 之前调用，`planner_agent.py:211,314-330`）

- 结合短期记忆 `get_session(user_id).get_context(max_turns=6)`（`:326`）将当前 query 改写为**自包含**形式，消解代词/指代（"它/这个/上个月/刚才说的产品"），使多轮 query（如"分析它的趋势"）以无歧义形式进入规划。
- 改写结果**仅用于规划**；对外标题/标签仍用原始 query。
- 复用 `get_chat_model(user_id)` 的按用户隔离 LLM；失败/无历史回退原始 query，不引入新硬失败。代价：每次分析多一次 LLM 调用（相对多分钟流水线可忽略）。

**② RAG 检索 `RetrievalQueryRewriter`**（`rag/retrieval_query_rewriter.py`，在 `retriever_docs` 粗召回前调用，`rag_service.py:56,143-154`）

- **多查询扩展**：生成 N=3 条语义相关但表述不同的改写，与原 query 一起多路粗召回、合并去重（`seen_ids`，`:144-152`），再喂给既有 rerank。
- rerank 仍用**原始 query** 打分（`:154`）--扩召回、保精度（rerank 只能排序粗召回已返回的结果，无法找回粗召回没取到的相关文档；故改写扩召回与 rerank 保精度不重复）。
- 失败回退 `[原始 query]`，检索行为退化为现状。

> 已知限制（ADR-0002）：短期记忆按 `user_id` 而非 `session_id` 索引，分析桥改写的历史可能跨会话泄漏（与 [§4](#4-记忆系统两层结构与当前限制) 短期记忆限制同源）。
