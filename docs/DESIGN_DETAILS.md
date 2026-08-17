# 核心设计深度剖析

## 1. 子代理编排：静态 `_agent_map` 与顺序派发

子代理采用**静态注册**：在 `PlannerAgent.__init__` 中实例化各阶段代理并把当前用户的 LLM **构造期注入**每个子 Agent（`SQLAgent(model=...)` 等，`planner_agent.py:107-128`），使整条流水线共享同一份按 `user_id` 隔离的模型配置（不再有构造后 `.model = ...` 回写）。其中 Trend / Product / Risk 三阶段已收敛为**同一个 `AnalysisAgent` 类**注入不同 `AnalysisModule` 适配器（`AnalysisAgent(TrendAnalysisAdapter())` 等，`planner_agent.py:111-113`），适配器封装各类型的列选择与计算、复用 `analysis/` 下纯计算类（`TrendAnalysis` / `ProductAnalysis` / `AnomalyDetection`），消除三个复制粘贴 Agent。旧 `TrendAgent` 类仍被 `quick_data_insight` @tool 使用（`agent_tools.py:201`），`ProductAgent` / `RiskAgent` 已删除。`product_analysis` 阶段已泛化为领域中立的**分组对比分析**：销售数据（含 price+qty 列）走"收入=单价×数量"快路径，人口/流量/运营等走"维度×度量"通用路径（按类别列分组、对数值列求和），不再对非销售数据强行 qty×price；`_detect_columns` 的 price/qty 仅按名列匹配，`build_product_summary` 输出 `dimension_col`/`measure_col`/`*_label` 元数据供报告渲染数据驱动表头（详见 CHANGELOG v0.4「流水线领域中性化」）。

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
> `_agent_map` 的键（handler 名）未变（`planner_agent.py:134-141`）；`trend` / `product` / `risk` 三个 handler 底层改用 `AnalysisAgent(adapter)`。另有 `DocumentReportAgent` **不在** `_agent_map` 中，仅由 `document_report` 工具按需懒加载调用。

**派发循环**（`planner_agent.py:242-288` / `:364-415`）是单线程顺序 `for step in plan:`，每个 `handler(task, pctx, ctx)` 同步调用、返回 `None`，结果写入**类型化 `PipelineContext`** dataclass（`agents/pipeline_context.py`）的槽位（`pctx.sql_result` / `pctx.trend_result` / `pctx.product_result` / `pctx.risk_result` / `pctx.visualization_result` / `pctx.report_result` / `pctx.export_result`），取代旧的 `prev_results` 字典与 `step_N` / `agent_name_result` 双键。最终返回的 `"results"` 直接持有 `PipelineContext` 实例本身（`planner_agent.py:285, 415`）。

**依赖与容错语义**（关键，需准确理解）：

- `depends_on` 现检查 `all(d in pctx.completed_steps for d in depends)`（`completed_steps: set[int]`，`planner_agent.py:251`）；若依赖未就绪，该步直接 `continue` **跳过**，不排队、不等待、不重试。成功后 `pctx.completed_steps.add(step_num)`（`:264`）。
- 单步抛异常时记入 `pctx.errors`、发出 `step_error` 进度；下游依赖该步的会被跳过（`planner_agent.py:274-278`）。
- **没有并行执行**：即便 `trend` 与 `product` 都只依赖 `sql_query`，也只能串行跑。
- **没有跨代理重试 / fallback**：唯一的重试是 `SQLAgent._fix_sql` 的错误回灌重生成（最多 2 次重试 = 3 次尝试）。
- `pctx.success`（`pipeline_context.py:54`）综合 `errors` 与各槽位判定。

**通信契约**：`SQLAgent` 产出的 `dataframe_json`（records-orient JSON 字符串）是主数据载体，写入 `pctx.dataframe_json`（`planner_agent.py:437`）后流入 `Trend` / `Product` / `Risk` / `Viz`；各阶段结果写入对应 `pctx.*_result` 槽位，`ReportAgent` 聚合全部槽位。`PipelineContext` 另暴露便利属性 `charts` / `report_markdown` / `export_files`（`pipeline_context.py:58-72`）供 `run_stream` 取用。

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

**IDOR 防护**：所有会话端点重新从 token 推导 `user_id`（绝不信任客户端），并与 `_long_term_memory.get_session_owner(session_id)` 比对，不匹配返回 **404（而非 403，防枚举）**--`/api/chat`（`api/routes/chat.py`）、`GET/DELETE/PATCH /api/sessions/{id}`（`api/routes/sessions.py`）。数据集删除额外校验 realpath 必须在 `_datasets_dir()` 内（`api/routes/datasets.py`）。

> 注意：`_duckdb_instances` 是**无上限的普通 dict**（无 LRU / TTL / 容量上限），高用户 churn 下存在内存增长风险，仅 `close_duckdb(user_id)` 可手动清理。

## 4. 记忆系统：两级记忆（ADR-0003，Session 级 + User 级）

记忆为**两级**设计（[ADR-0003](adr/0003-two-tier-memory-session-and-user-scoped.md)），由 `MemoryService` 外观（`memory/service.py`）统一编排为 `begin_turn()` / `end_turn()` 两方法，调用方不再直接操作底层模块：

- **Session Memory**（`memory/short_term.py`）：按 `session_id` 隔离（不再按 `user_id`），进程内 LRU 池 `_session_pool`，miss 时从 SQLite `conversation_history` **回灌**（`_ensure_hydrated`）。压缩不再用固定 30 轮，改为 **90% 上下文预算触发**：`_maybe_compress` 经 `usage_metadata` 的 `input_tokens` 判定（字符兜底 80%），折半折叠并写入 `summarized_up_to` **水印**持久化到 `chat_sessions` 表。`MAX_TURNS=30` 现仅作非聊天路径（如共指改写 `get_context(max_turns=6)`）的默认截断。
- **Long-Term Memory**（`memory/long_term.py`）：SQLite `database/memory.db`（`memory_summaries` / `chat_sessions` / `conversation_history`，已加 `session_id` 列）+ **跨会话召回** `MemoryRecallService`（`memory/recall.py`）：终版会话摘要写入 ChromaDB `memory` collection（shared-collection + `user_id` owner 过滤，`include_public=False`），按相关性检索 + `gte-rerank-v2` 精排。闲置会话（`SESSION_IDLE_SECONDS`）的终版摘要 piggyback 到下次请求后台生成。
- **召回注入**：经 `dynamic_prompt` 中间件（`report_prompt_switch` -> `_build_system_prompt`）在**正常模式**把跨会话召回注入系统提示（报告模式不注入）并限长；upsert 原子串行化。
- **循环依赖打破**：`ConversationSummarizer` 改为构造时注入 `llm_callable`（不再 `import BaseAgent`），`MemoryService` 经 `set_summarizer_factory` 注入按 `user_id` 构造的 summarizer，消除 `memory` -> `agents` -> `memory` 环。

> ⚠️ **当前限制（经核验）**：
> - `MemoryService` 为进程级单例（`api/deps.py 的 _get_memory_service`），summarizer 的 LLM 在首次调用时按首个 `user_id` 构造并缓存，后续不同用户的摘要压缩复用同一 LLM（会话**数据**仍按 user/session 隔离，仅摘要模型的配置隔离不成立）。
> - `PipelineContext.dataframe` 共享反序列化属性已定义但未启用，`AnalysisAgent` 仍各自 `pd.read_json`。
> - `planner_agent._rewrite_query` 与 `middleware._recall_for_turn` 仍直连 `memory.short_term` / `memory.recall` 子模块，未走 `MemoryService`。
> - 长期记忆无 TTL / 遗忘机制，`conversation_history` 持续增长。

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
- **热重载**：`reload_model_config(user_id)` 直接 `pop` 两个缓存条目（`:111-119`），下次 `get_chat_model` 重建。设置保存端点还会调 `_invalidate_user_agents(user_id)`（`api/deps.py`）丢弃该用户的 Agent 实例。**注意：没有"版本号"机制**--是"保存即清缓存、取用时重建"的失效模式。
- **配置优先级**（`factory.py:65-92`，已核验）：用户网页配置 > `.env` 环境变量 > YAML 默认值。
- **API Key 加密**：在 `database/user_settings_db.py` 而非 factory。`_get_fernet()`（`:25-49`）从 `INSIGHTFORGE_SETTINGS_KEY` 取主密钥；缺失则 `Fernet.generate_key()` 随机生成并追加写入 `.env`（`load_dotenv(override=False)` 先加载避免覆盖既有密钥）。保存时 `f.encrypt`、读取时 `f.decrypt`，前端 `get_masked` 返回 `sk-***456` 形式。

## 7. SSE 流式与跨线程进度推送

`/api/chat` 返回 `StreamingResponse(media_type="text/event-stream")`，每条事件为 `data: <payload>\n\n`，并设 `X-Accel-Buffering: no` 禁用 nginx 缓冲（`api/routes/chat.py`）。

**线程->异步桥** `_stream_with_heartbeat`（`api/sse.py`）：`ReactAgent.execute_stream` 是同步生成器，在 `run_full_analysis` 等长工具期间会阻塞数分钟。为不饿死异步循环，它被放进**守护线程**跑，每个 chunk 经 `loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))` 推入无界 `asyncio.Queue`；主协程 `await asyncio.wait_for(queue.get(), timeout=15)`，超时则 `yield` 一个心跳保活。线程异常装入 `error_box` 在 `finally` 抛回主协程 -> `[ERROR]`。

**跨线程进度**：`ProgressEmitter.bind(loop, queue)`（`:48`）共享同一队列；`PlannerAgent` 经 `contextvars` 取到对应 emitter（`planner_agent.py:184`），`emit` 用 `loop.call_soon_threadsafe` 把步骤事件推入队列，从而绕过被阻塞的生成器直达前端。

**SSE 协议 token**：

| Token | 产出位置 | 含义 |
|-------|----------|------|
| `[SESSION]{id}` | `api/routes/chat.py` | 当前轮会话 ID |
| `[SESSIONS_RELOAD]` | `api/routes/chat.py` | 新建会话时通知前端刷新列表 |
| `[KEEPALIVE]` | `api/sse.py`（心跳串由 chat 路由传入） | 15s 心跳，前端仅重置空闲计时 |
| `[STEP:{json}]` | `api/routes/chat.py`（事件源为 `ProgressEmitter`） | 步骤进度（plan / step_start / step_done / step_error / status） |
| `[THINKING]{text}` | `agent/react_agent.py` | 工具调用提示，不计入持久化内容 |
| `[CHART:{url}]` | `api/routes/chat.py` | 检测到新生成的图表 HTML |
| `[DONE]` | `api/routes/chat.py` | 流结束 |
| `[ERROR] {msg}` | `api/routes/chat.py` | 流式异常 |

> `[CONTEXT]` 与 `[AUDIT:]` 在前端 `app.js` 有解析分支，但**后端无任何产出方**——为预留的 dormant 分支。审计通道目前未启用。

> **协作式服务端取消**（`utils/cancel_token.py`）：主协程在心跳超时与每 20 个 chunk 处抽检 `request.is_disconnected()`，断连即置 `CancelToken`（event + contextvar），`ReactAgent` 流循环与 `PlannerAgent` 步骤边界轮询退出，`run_full_analysis` 不吞取消异常；取消后不发 `[DONE]`、不写记忆。**不抢占**：单次进行中的 LLM 调用仍会完成，取消发生在下一个边界。

## 8. 文件分轨：文本入向量库 / 表格入 DuckDB

上传按扩展名自动分轨，前端 `_routeFileByExt`（`app.js:1116-1120`）决定路由：

- **表格类**（`csv` / `xlsx` / `xls`）-> `POST /api/datasets/upload` -> DuckDB 建表 + `datasources.db` 记元数据（`owner_user_id` 隔离），可直接 SQL / 跨表 JOIN。
- **文本类**（`txt` / `pdf` / `docx` / `md`）-> `POST /api/knowledge/upload` -> `VectorStoreService.load_single_document` 增量入 ChromaDB。

`GET /api/files`（`api/routes/knowledge.py`）合并两类返回统一列表；删除按 `type` 在前端分流到 `DELETE /api/datasets/{name}`（drop 表 + 删文件 + 删元数据）或 `DELETE /api/knowledge/files/{filename}`（删向量分片 + 删文件）。文件解析：PDF 用 `PyPDFLoader`（仅文本，无 OCR）、DOCX 用 `python-docx`（段落 + 表格按 `|` 拼接）、TXT/MD 用 `TextLoader`（`utils/file_handler.py`）。

## 9. Query Rewriting（两点改写，ADR-0002）

系统原本把原始 query 直接送入分析流水线与 RAG 检索，不做变换。现新增两个**独立改写组件**（均非 `BaseAgent` 子类），因两处需要不同变换（[ADR-0002](adr/0002-query-rewriting-two-points.md)）：

**① 分析桥 `QueryRewriter`**（`agents/query_rewriter.py`，在 `PlannerAgent.run` 的 `_create_plan` 之前调用，`planner_agent.py:211,314-330`）

- 结合短期记忆 `get_session(session_id, user_id).get_context(max_turns=6)`（`planner_agent.py:322`）将当前 query 改写为**自包含**形式，消解代词/指代（"它/这个/上个月/刚才说的产品"），使多轮 query（如"分析它的趋势"）以无歧义形式进入规划。
- 改写结果**仅用于规划**；对外标题/标签仍用原始 query。
- 复用 `get_chat_model(user_id)` 的按用户隔离 LLM；失败/无历史回退原始 query，不引入新硬失败。代价：每次分析多一次 LLM 调用（相对多分钟流水线可忽略）。

**② RAG 检索 `RetrievalQueryRewriter`**（`rag/retrieval_query_rewriter.py`，在 `retriever_docs` 粗召回前调用，`rag_service.py:56,143-154`）

- **多查询扩展**：生成 N=3 条语义相关但表述不同的改写，与原 query 一起多路粗召回、合并去重（`seen_ids`，`:144-152`），再喂给既有 rerank。
- rerank 仍用**原始 query** 打分（`:154`）--扩召回、保精度（rerank 只能排序粗召回已返回的结果，无法找回粗召回没取到的相关文档；故改写扩召回与 rerank 保精度不重复）。
- 失败回退 `[原始 query]`，检索行为退化为现状。

> 该限制（ADR-0002 原述：短期记忆按 `user_id` 而非 `session_id` 索引，分析桥改写历史可能跨会话泄漏）**已由 ADR-0003 解决**：Session Memory 现按 `session_id` 隔离（见 §4）。
