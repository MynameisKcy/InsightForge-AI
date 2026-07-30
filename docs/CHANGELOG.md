# 版本更新记录

### v0.3（2026-07-30）

> 相对 v0.2 的主要更新。结论基于源码与提交记录核验。

**架构定型**
- 单入口架构定型（[ADR-0001](adr/0001-single-entry-analysis-as-tool.md)）：ReactAgent 为唯一入口，分析流水线作为 `run_full_analysis` 工具调用；直连 `/api/analysis` 退役（路由保留、前端不调）。
- 术语统一为 Smart Assistant / Analysis Pipeline（见 `CONTEXT.md`），不再称"数据分析模式"。

**查询改写（[ADR-0002](adr/0002-query-rewriting-two-points.md)）**
- 分析桥 `QueryRewriter`：`PlannerAgent.run` 规划前结合短期记忆消解对话指代，多轮 query 以自包含形式进入规划。
- RAG 检索 `RetrievalQueryRewriter`：粗召回前多查询扩展（N=3），合并去重后喂给既有 rerank（仍用原 query 打分），扩召回保精度。

**导出**
- `_run_export` 不再硬编码 `["md","html"]`：按用户请求文本解析格式（`_resolve_export_formats`），未指定时回退 `md+html`。

**工程**
- 测试套件扩充至 139 passed / 20 文件（新增查询改写、planner 调用、analyst 缓存、向量库隔离等用例）。
- 清理一次性临时脚本 `_filter_check.py`；README 全面校准 `planner_agent.py` / `rag_service.py` / `fastapi_server.py` 行号引用并补结构树。
- README 重构为精简版（简介 / 亮点 / 技术栈 / 快速开始），原详细章节迁移至 `docs/` 下 8 个独立文档（架构 / 设计 / 结构 / 配置 / API / 安全 / 测试 / 更新记录）；GitHub 仓库更名为 InsightForge-AI。

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
- 知识库"已入库但读不到"：根因为 `md5.text` 与 chroma 实际状态偏离（如 chroma_db 被删而 md5 残留），文件永久卡死。修复后以 chroma 实际分片为"可读"真相，`_ingest_if_needed` 在偏离时自愈重灌，列表 `ingested` 状态不再仅凭 md5。

**工程**
- 测试套件扩充至 101 passed（新增知识库自愈、鉴权、配置优先级、工厂等用例）。
- 移除废弃 `app.py` / SDD 文档；新增 `scripts/repo_cleanup.sh`。
- `.gitignore` 补齐知识库上传文件（pdf/docx）。

### v0.1

初始可用版本：多智能体协作数据分析平台（智能客服 + 数据分析双模式）、NL->SQL 只读沙箱、多用户隔离、RAG 两阶段检索、DuckDB 多源、多格式报告导出。
