# InsightForge AI — 简历项目总结（STAR 法则）

> 多智能体协作数据分析平台 · 个人独立设计开发 · LangChain + LangGraph
> 本文件含**段落版**（简历项目介绍，约 287 字）、**完整版**（详细 STAR，适合作品集 / 项目深挖页）、**简洁版**（直接贴简历的 3–4 条）、**英文版**与**面试深挖点**。

---

## 一、项目元信息（一句话背书）

**一句话定位**：上传任意数据，用自然语言提问，AI 智能体自主编排「SQL 查询 → 多维分析 → 可视化 → 多格式报告」全链路，业务人员零代码自助取数、数据分析师提效 10×。

| 维度 | 内容 |
|------|------|
| 技术栈 | LangChain、LangGraph、FastAPI、DuckDB、SQLite、ChromaDB、DashScope（Qwen）、Plotly + Kaleido、Jinja2 |
| 代码规模 | ~15,000 行 Python，9 个子模块（agents / analysis / memory / rag / database / api / visualization / model / utils） |
| 质量保障 | 258 个测试用例全通过，34 个测试文件，覆盖 SQL 沙箱、多用户隔离、记忆压缩、领域中立分析等核心路径 |
| 架构治理 | 3 份 ADR（架构决策记录）驱动演进，ADR-0001 单入口 / ADR-0002 查询改写 / ADR-0003 两级记忆 |
| 角色 | 独立开发者（全栈：架构设计、后端、LLM 编排、前端、测试、文档） |

**简历版项目介绍（段落式 · 约 287 字 · 适合 HR + 技术面试官）**

独立设计并全栈实现的自然语言数据分析平台。
基于 LangChain + LangGraph 搭建 7 智能体协作流水线（SQL → 趋势/分组/风险分析 → 可视化 → 报告 → 导出），并叠加 ReAct 智能客服双模式。
核心技术：自研两级记忆架构（会话级隔离 + 跨会话召回，按 token 预算 90% 阈值触发压缩）、基于 `contextvars` 的多用户隔离、只读 SQL 沙箱与两阶段 RAG 检索（ChromaDB + gte-rerank-v2 精排）。
后端 FastAPI + SSE 流式输出，数据层 DuckDB/SQLite/ChromaDB 多库分工，支持 CSV/Excel/MySQL/PostgreSQL 多源接入与跨表 JOIN。独立完成约 1.5 万行代码、258 项单元测试全部通过，沉淀 3 份架构决策记录（ADR）。

---

## 二、完整版（详细 STAR）

### 🟦 Situation（背景）

企业数据分析长期存在两道门槛：① 业务人员想看数却不会写 SQL，依赖数据部门排期；② 数据分析师被重复取数、套模板画图、写周报消耗大量精力。市面上的 BI 工具仍需拖拽配置、手动选图表类型，没有真正打通「自然语言 → 自动洞察 → 可交付报告」的闭环。同时，多租户 SaaS 场景对**查询安全**（防 SQL 注入/越权）、**数据隔离**（用户间数据不可串）、**长对话记忆**（上下文不爆且不丢关键信息）提出了硬性工程要求。

### 🟩 Task（任务）

独立设计并实现一个多智能体协作数据分析平台 **InsightForge AI**，目标：

1. **自然语言端到端**：用户上传 CSV/Excel 或接入 MySQL/PostgreSQL，一句话即可完成查询、分析、画图、导出报告，全程零代码。
2. **多智能体自主编排**：由 LLM 根据意图自动路由——直接问答 / RAG 知识库 / 触发完整分析流水线，而非用户手动选「分析类型」。
3. **工程级健壮性**：只读 SQL 沙箱防注入、按用户隔离的内存 OLAP、两级记忆系统支持长对话、258 测试用例保障可维护性。
4. **领域中立**：不预设「销售/利润」语义，对人口、流量、运营等任意维度数据都能正确分析。

### 🟧 Action（行动 · 技术落地）

**1. 多智能体编排：单入口 + LLM 自主路由（ADR-0001）**
- 基于 LangGraph `create_agent` 构建唯一入口 ReactAgent，绑定 **15 个 `@tool` 工具 + 3 个中间件**（监控、日志、动态提示词切换），由 LLM 读工具描述自主决策调用路径，一句话触发全链路。
- 分析流水线由 `PlannerAgent` 用 LLM 生成 JSON 执行计划（含 `depends_on` 依赖），再**顺序派发** 7 阶段子代理：`SQLAgent → AnalysisAgent(趋势/分组/异常) → VisualizationAgent → ReportAgent → ExportAgent`。
- 设计类型化 `PipelineContext` dataclass 取代松散字典传递中间结果，以 `completed_steps` 集合管控依赖与容错（依赖未就绪则跳过、单步异常不阻断全链路）。
- 将趋势/分组/异常三类分析收敛为**单一 `AnalysisAgent` 类 + 可插拔 `AnalysisModule` 适配器**（纯 pandas/numpy 计算、零 LLM），消除三份复制粘贴代码。

**2. 安全与隔离：AST 级 SQL 沙箱 + contextvars 多租户**
- 用 **sqlglot 把 LLM 生成的 SQL 解析为 AST**（`sqlglot.parse(sql, read="duckdb")`），执行前三道校验：多语句拒绝（防 `SELECT; DROP`）、语句类型白名单+黑名单双保险、函数级黑名单（拦截 `read_csv_auto`/`httpfs`/`system`/`shell` 等文件/网络/执行函数）；表名经 `safe_ident` 双引号转义防注入。**33 个沙箱测试用例**专项覆盖。
- 用 **`contextvars` 在调用链透传 `user_id`/`session_id`**，实现按用户隔离的 DuckDB `:memory:` OLAP 实例、按用户缓存的 LLM/Embedding、数据集元数据按 `owner_user_id` 过滤——多用户数据彻底隔离。

**3. 两级记忆系统（ADR-0003）：会话隔离 + 跨会话语义召回**
- **Session 级**：按 `session_id` 隔离，LRU 池 + DB 漏填回灌，**以 90% 上下文预算为阈值**（非固定轮次）触发压缩，用 `summarized_up_to` 水位线避免重复摘要。
- **长期记忆**：SQLite 持久化对话历史 + ChromaDB `memory` 集合做跨会话语义召回（`gte-rerank-v2` 精排），召回结果在 `dynamic_prompt` 中间件注入系统提示词。
- 以依赖注入打破 `ConversationSummarizer ↔ agents` 循环依赖，`MemoryService` 门面统一 `begin_turn()/end_turn()` 生命周期。

**4. 数据接入与检索增强**
- 抽象**统一数据源层**：CSV/Excel/MySQL/PostgreSQL 异构源全部载入 DuckDB，支持**跨源 JOIN**；脏数据在接入层自动清洗（非法列名/全角/重名），不让用户重传。
- **两阶段 RAG**：ChromaDB 粗召（k=15）→ DashScope `gte-rerank-v2` 精排（top_n=3、阈值 0.3），状态码非 200 自动降级容错。
- **列级 Schema 语义画像**：自动计算每列统计 + 宽表检测，注入 NL2SQL 提示，提升生成 SQL 准确率；画像结果按实例缓存、表重建时失效。

**5. 流式交付与可视化工程**
- 设计 FastAPI **SSE 流式协议**（`[STEP]`/`[CHART]`/`[KEEPALIVE]`/`[DONE]` 等令牌）+ `ProgressEmitter` 跨线程推送，前端实时渲染步骤进度与图表。
- 图表交互态用 Plotly HTML，导出态光栅化为 PNG 嵌入 **Word/MD/PDF/HTML 四格式报告**；定位并修复 kaleido 同步服务器在进程内第二次调用因 GIL 看门狗被杀的 bug——改用**常驻同步服务器**（`start_png_batch`/`write_fig_sync`），避免每次新建 scope 的开销与挂起。

**6. 架构治理与质量**
- 以 **ADR 驱动**关键决策，持续推进重构：抽离 `safety.py`/`schema.py`/`customer_profiles.py` 统一接缝、收口 profile-cache 失效钩子、删除 `ProductAgent`/`RiskAgent` 死模块。
- **258 个测试用例全通过**，覆盖安全沙箱、多用户隔离、记忆压缩、领域中立分析、图表生成、导出等核心路径，保障重构不回归。

### 🟥 Result（成果）

- ✅ **端到端闭环可用**：从自然语言提问到 4 格式报告导出全链路自动完成，业务人员零代码自助取数。
- ✅ **安全可信**：AST 级只读沙箱 + 标识符转义 + 按用户隔离，多租户下查询安全与数据隔离有测试保障（33 沙箱用例）。
- ✅ **工程深度**：~1.5 万行代码、9 大子模块、258 测试全过、3 份 ADR，体现可测试性、可维护性与架构演进能力。
- ✅ **领域泛化**：分组对比分析从「销售专用」泛化为领域中立，对人口/流量/运营数据均正确出图出表。
- ✅ **可展示性**：双主题（暗夜/暖阳）落地页 + 工作台 + 交互式图表 + 多格式报告，演示链路完整。

---

## 三、简洁版（直接贴简历 · 4 条）

> **InsightForge AI — 多智能体协作数据分析平台**（个人项目 · 独立开发）
> 技术栈：LangChain / LangGraph / FastAPI / DuckDB / ChromaDB / DashScope / Plotly

- **多智能体编排**：基于 LangGraph 设计「单入口 ReactAgent + LLM 自主路由 15 工具 + 7 阶段分析流水线（SQL→多维分析→可视化→报告导出）」架构，LLM 生成 JSON 执行计划并顺序派发子代理，用类型化 `PipelineContext` 管控依赖与容错；代码规模约 1.5 万行、258 个测试用例全通过。

- **查询安全与多租户隔离**：用 sqlglot 将 LLM 生成的 SQL 解析为 AST，实现多语句拒绝 + 语句/函数黑白名单 + `safe_ident` 标识符转义的只读沙箱（33 个专项测试）；以 `contextvars` 透传用户上下文，实现按用户隔离的 DuckDB 内存 OLAP、LLM 缓存与数据集权限过滤。

- **两级记忆 + 两阶段 RAG**：设计 Session 级隔离（90% 上下文预算触发压缩 + 水位线去重）与跨会话语义召回（ChromaDB + gte-rerank-v2 精排）两级记忆系统；RAG 采用 ChromaDB 粗召 k=15 → 精排 top_n=3 的两阶段检索，提升长对话连贯性与检索准确率。

- **异构数据源统一接入 + 架构治理**：抽象统一数据源层支持 CSV/Excel/MySQL/PostgreSQL 载入 DuckDB 并跨源 JOIN，结合列级 Schema 语义画像自动注入 NL2SQL 提示；以 3 份 ADR 驱动架构演进（单入口、查询改写、两级记忆），持续重构消除死代码、统一安全接缝。

---

## 四、英文精简版（Bonus · 应对外企/英文简历）

**InsightForge AI — Multi-Agent Collaborative Data Analysis Platform** (Personal Project · Solo Developer)
*LangChain · LangGraph · FastAPI · DuckDB · ChromaDB · DashScope (Qwen) · Plotly*

- **Multi-agent orchestration**: Built a single-entry ReAct agent with LLM-autonomous routing across 15 tools and a 7-stage analysis pipeline (SQL → multi-dimensional analysis → visualization → report export); LLM generates JSON execution plans dispatched sequentially with a typed `PipelineContext` for dependency control and fault tolerance. ~15K LOC, 258 passing tests.

- **SQL sandbox & multi-tenant isolation**: Implemented an AST-level read-only SQL sandbox via sqlglot (multi-statement rejection, statement/function blacklists, identifier escaping) backed by 33 dedicated tests; enforced per-user isolation of in-memory DuckDB OLAP, LLM caches, and dataset ACLs via `contextvars`.

- **Two-tier memory & two-stage RAG**: Designed a session-tier memory (auto-compression at 90% context budget with watermark de-dup) plus cross-session semantic recall (ChromaDB + gte-rerank-v2 rerank); RAG uses ChromaDB coarse retrieval (k=15) → rerank (top_n=3) for higher accuracy and long-dialog coherence.

- **Unified heterogeneous ingestion & ADR-driven architecture**: Abstracted a unified data-source layer ingesting CSV/Excel/MySQL/PostgreSQL into DuckDB with cross-source JOIN and column-level schema profiling auto-injected into NL2SQL prompts; drove architecture evolution through 3 ADRs, continuously refactoring to remove dead code and centralize security seams.

---

## 五、面试可深挖点（备查）

- 为什么用 LangGraph `create_agent` 而非旧版 `AgentExecutor`？（流式、工具调用语义更原生）
- SQL 沙箱为什么选 AST 级而非进程隔离？局限是什么？（同进程、无资源上限，生产需前置限流）
- 90% 上下文预算压缩如何避免丢关键信息？（水位线 `summarized_up_to` + 增量摘要）
- kaleido 第二次调用挂起的根因？（每次 `write_image` 新建 scope，watchdog 杀进程）如何解决？（常驻同步服务器）
- 多用户隔离为什么用 `contextvars` 而非线程局部？（async/跨 await 透传，线程局部会丢）
- 两阶段 RAG 为什么粗召 k=15？（先高召回，再靠 rerank 提精度，平衡延迟与准确率）
