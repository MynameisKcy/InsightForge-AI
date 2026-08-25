# 版本更新记录

### 未发布

- refactor(api): 聊天流线协议出路由（架构评审第2轮候选2）——新增 `api/chat_stream.py` 收口 /api/chat 的 SSE 线协议全权实现（preamble token/进度事件路由/THINKING 切片/句子节奏/断连采样心跳必检+每20chunk抽检/图表 diff 下发与正文嵌入/end_turn 持久化+取消路径三不做），依赖（agent/memory_service/is_disconnected/charts_dir）显式参数注入；route 薄化为请求解析+auth+接缝+StreamingResponse 组装（192→57 行）；`deps.begin_memory_turn` 收口 chat/analysis 两路由的 PermissionError→404 括号。行为字节级不变（0.06s/0.03s 节奏、采样频率、TestClient 断连误报规避均保留）；新增 tests/test_chat_stream_pipeline.py 15 例直测（断连采样/取消路径等端点测试够不着的分支）
- refactor(export): 报告 Markdown 方言解析收口——新增 `agents/markdown_blocks.py` 纯函数块解析器(标题/竖线表格/图片引用/列表/分隔线/空行 → Block, 零 FS 访问), `ExportAgent` Word/PDF/HTML 三份遍历器折叠为其上的薄渲染 adapter, 行内标记转换语义留各格式; 顺修三处副本漂移: Word 对 `---` 渲染分隔线(原字面横杠段落)、表格分隔行判定统一为逐格含 `--`(原 Word/PDF 整行子串匹配)、HTML 图片引用解析失败改输出占位文字(原输出破图 `<img>`); 无分隔行表格 HTML 表头/表体归位(原全渲染成 th)。MD 直通内联路径不变
- refactor(cleanup): 删 fastapi_server 兼容再导出层——测试引用迁至属主模块(`api.sse._stream_with_heartbeat` / `api.deps._get_vector_store` / `database.user_settings_db`), `conftest._swap` 收敛为 deps 单属主(缺名即断言失败), auth 导入缩减为实际使用项; `.gitignore` 删已不存在的 plans/specs/superpowers 条目并忽略本机产物(`tmp-kaleido/` `.tmp/` `.zcode/` `*.db.bak`)
- refactor(api): 拆分 fastapi_server 巨石——1252 行组装根收敛为 ~110 行 + `api/deps.py`(服务接缝) + `api/sse.py`(线程->异步流式桥) + `api/serialization.py` + `api/routes/` 八模块(chat/analysis/datasets/knowledge/sessions/settings/users/pages)；测试换桩目标从 `api.fastapi_server` 迁至属主模块(conftest `_swap` 优先 `api.deps`)
- refactor(architecture): 架构重构 Tier1 收敛(候选1-8)——database 抽 `safety.py`/`schema.py`/`customer_profiles.py` 统一 SQL 安全与画像接缝, profile-cache 收口 `_invalidate_profile()`; `MemoryService` 外观收口记忆三层操作(会话读/改名/删除协调 + `_assert_owner` IDOR 前置); rag 抽共享 `rerank_docs`(gte-rerank-v2) 供 recall/rag_service 复用; `BaseAgent`/子 Agent/`PlannerAgent` 构造期模型注入(删 reach-around 赋值); 洞察回退契约归位各 `AnalysisModule` 适配器(删 9 键并集硬编); 删死模块 ProductAgent/RiskAgent 与 `PipelineContext.dataframe` 死属性
- refactor: 扁平化包根(agent/ 整层上移至 repo 根), 统一导入形式, 消除 63 处双导入兜底(余 2 处单臂可选导入守卫)与 63 处散落 sys.path hack
- fix(user-model): 按用户解析模型传播收尾——quick_data_insight/TrendAgent/ExportAgent/DocumentReportAgent 构造补传 user_id, RAG 摘要链 `_get_chain(user_id)`, MemoryService 改 `llm_factory(user_id)` + summarizer 按用户工厂(删 `_memory_llm_user` 共享字典, 消后台闲置 finalize 线程竞态)
- test(api): 补端点测试盲区——chat SSE token 契约/analysis/datasets/datasources reload/knowledge reindex, 新增 tests/conftest.py 共享 fixtures(+31 用例)
- feat(security): DuckDB 查询通道资源上限——`config/agent.yml` `duckdb` 节(memory_limit/threads 经连接配置, max_result_rows 超限报错喂 `_fix_sql` 自愈, query_timeout watchdog `conn.interrupt()` 中断); 沙箱白名单零放宽(SET/PRAGMA 依旧被拒)
- feat(sse): 客户端断连的协作式服务端取消——`utils/cancel_token.py`(event+contextvar); /api/chat 心跳必检+每 20 chunk 抽检断连, ReactAgent 流循环与 PlannerAgent 步骤边界退出, run_full_analysis 不吞取消异常; 取消后不发 [DONE]/不写记忆。顺手修 progress_emitter/cancel_token 的 `token.reset()` 误用(reset 是 ContextVar 方法, 旧写法恒 AttributeError 被吞)

### v0.5（2026-08-06）

> 相对 v0.4 的主要更新。

**报告导出图表修复（Word 崩溃 + 三格式无图）**
- 根因：图表为 Plotly 交互式 `.html`，报告嵌入其 FS 路径 `![图](…/foo.html)`。Word `add_picture` 对 HTML 抛 `UnrecognizedImageError` 致整条导出失败；PDF 硬编码「图表见 HTML 版报告」占位；HTML/MD 的 `<img>`/`![]()` 指向 HTML 文件无法渲染。
- `visualization/charts.py`：`_save_chart` 写 HTML 后 best-effort 用 kaleido 生成同名 `.png`（失败仅告警，不阻断图表生成）；新增 `chart_png_path(html_path)` 查同名 PNG。**关键**：不用 Plotly 的 `fig.write_image()`（同进程第 2 次调用新建 scope 会挂起），改用 `kaleido.start_sync_server` + `write_fig_sync` 复用单一 chromium scope；server 进程内常驻不 stop（`stop_sync_server` 在解释器退出触发 GIL 致命错误），`start_png_batch`/`stop_png_batch`（no-op）暴露批次语义。
- `agents/visualization_agent.py`：chart entry 增 `png_path` 字段，经 `PipelineContext.charts` 贯通至 ReportAgent。
- `agents/report_agent.py`：`_build_report_data` 嵌入 PNG 的 Web URL（`/reports/charts/foo.png`）替代 FS 路径，报告 bubble 亦能渲染图（前端 `safeUrl` 放行 `/` 开头）；新增 `_chart_web_url`。
- `agents/export_agent.py`：新增 `_resolve_chart_image`（Web URL/FS/`.html` 同名 PNG 解析）/`_png_data_uri`/`_scaled_image`（Pillow 保比缩放）。Word `add_picture` 包 try/except 不再崩；PDF 嵌入 `reportlab.Image` 替代占位文字；HTML/MD 内联 base64 data URI（自包含可离线）。
- `requirements.txt`：新增 `kaleido`、`Pillow`。
- 新增 `tests/test_export_images.py`（10 用例：解析器各分支 + 四格式嵌图 + Word 不崩）。

**README 美化**
- 参照 beautify-github-readme 重写：shields.io 徽章组、目录锚点、`<details>` 折叠安装步骤与文档列表、居中截图布局、emoji 章节标题、页脚签名。

### v0.4（2026-08-05）

> 相对 v0.3 的主要更新。结论基于源码与提交记录核验。

**两级记忆（[ADR-0003](adr/0003-two-tier-memory-session-and-user-scoped.md)，四阶段已落地）**
- Session Memory 按 `session_id` 隔离 + LRU 池 + DB 回灌（miss 时从 `conversation_history` 重建）+ `summarized_up_to` 水印；压缩由固定 30 轮改为 **90% 上下文预算触发**（`usage_metadata` + 字符兜底 80%，折半折叠）。
- Long-Term Memory 新增跨会话召回：终版会话摘要写入 ChromaDB `memory` collection（shared-collection + `user_id` owner 过滤，`include_public=False`），按相关性检索 + `gte-rerank-v2` 精排。
- 闲置会话终版摘要（`SESSION_IDLE_SECONDS`）piggyback 到下次请求；召回注入移入 `dynamic_prompt` 中间件（正常模式注入、报告模式不注入）并限长；upsert 原子串行化。
- `MemoryService` 外观（`memory/service.py`）将分散在 fastapi_server / react_agent / planner_agent / middleware 的记忆操作收敛为 `begin_turn()` / `end_turn()`，编排 Session Memory + Long-Term Memory + Recall 三层。
- 打破 `memory ↔ agents` 循环依赖：`ConversationSummarizer` 改为注入 `llm_callable`，不再导入 `BaseAgent`；`MemoryService` 经 `set_summarizer_factory` 注入按 `user_id` 构造的 summarizer。

**管道架构重构**
- 统一 `AnalysisAgent` + `AnalysisModule` Protocol（`analysis/analysis_module.py`）：趋势 / 产品 / 风险三阶段收敛为单个 `AnalysisAgent` + 三个适配器（`TrendAnalysisAdapter` / `ProductAnalysisAdapter` / `RiskAnalysisAdapter`），复用既有 `TrendAnalysis` / `ProductAnalysis` / `AnomalyDetection` 计算类，消除三个复制粘贴 Agent。
- `PipelineContext`（`agents/pipeline_context.py`）类型化 dataclass 替代 `prev_results` 字典抓取袋：handler 改写 `pctx.*_result` 类型化槽位，`completed_steps: set[int]` 依赖门控，便利属性 `charts` / `report_markdown` / `export_files`。`step_N` / `agent_name_result` 键整体移除。
- 注：`TrendAgent` 类保留，仍被 `quick_data_insight` @tool 使用；`ProductAgent` / `RiskAgent` 已无活跃调用方。

**流水线领域中性化（不再锁死销售场景）**
- `product_analysis` 阶段语义改为**分组对比分析**（领域中立）：销售数据（含 price+qty 列）走“收入=单价×数量”快路径，人口/流量/运营等任意数据走通用路径（按类别维度分组、对数值度量列求和），不再对非销售数据强行 qty×price。`_detect_columns` 的 price/qty 改为仅按名列匹配（不再数值兜底误乘）；`build_product_summary` 输出 `dimension_col`/`measure_col`/`*_label` 元数据供报告渲染表头。
- `AnomalyDetection` 度量列自适应（无 price/qty 时取首选数值列），输出键 `revenue_anomalies`→`measure_anomalies`、异常项 `revenue`→`value`；方法 `detect_revenue_anomalies`→`detect_measure_anomalies`、`detect_category_loss`→`detect_low_performers`、`detect_location_anomalies`→`detect_group_anomalies`。
- 报告模板 `report_template.md` 与 `_basic_markdown_report` 改用数据驱动表头（`{{ dimension_label }}`/`{{ measure_label }}`/`p[dimension_col]`），不再硬编码“产品/总收入/销量”；空结果段加 `{% if %}` 守卫。报告/可视化/规划器/工具提示词去销售化（“商业分析师”→“数据分析师”、“商业洞察”→“数据洞察”，示例跨销售+人口+流量）。
- 内部标识符（agent key `product_analysis`、槽 `product_result`、类名 `ProductAnalysis`/`ProductAnalysisAdapter`）保留以最小化 churn，仅用户可见标签改为“分组对比分析”。
- 新增 `tests/test_domain_neutral.py`（7 用例：人口/流量数据分组对比 + 风险度量异常 + 计划路由）。

**报告导出**
- 新增 `POST /api/report/export` 端点 + `ExportAgent`，支持 Word / MD / PDF / HTML 下载。
- PDF 注册 Windows 中文字体 + 渲染表格 + 图片占位，修复中文乱码与表格丢失；前端报告流结束后渲染导出按钮。历史会话消息现与实时流式一致地渲染 markdown / 图表 / 导出按钮。

**Schema 语义画像**
- `_compute_table_profile` 计算列语义统计 + 宽表检测；`get_enhanced_schema_text` 输出列语义统计 + 宽表标记 + 实例级 `_profile_cache`；重建同名表时清缓存防 stale profile。

**健壮性**
- Trend：入口选数值列、全文本列直接返回提示；`build_trend_summary` 对非数值列 `coerce` 防御。
- 图表：pie / bar 画图前剔除汇总行（全省 / 总计等），避免汇总混入各市对比占大头。
- CSV 解码：`load_csv_dataset` 与管理通道 `_load_csv` 在 `read_csv_auto` 失败时均回退 pandas 按 GBK / GB18030 / UTF-8 解码，修复非 UTF-8 中文 CSV 上传报错。

**数据集命名**
- 数据集保留中文原名 `display_name`，侧边栏显示原名 + 批量上传映射；`DataResolver` 按原名定位（用户输入"山东"命中"山东省..."）。

**工程**
- 测试套件扩充至 **216 passed / 29 文件**（新增两级记忆 / 召回、导出端点与 Agent、Schema 画像、AnalysisAgent / PipelineContext、display_name、上下文预算等用例）。
- 已知实现落差：~~`PipelineContext.dataframe` 未启用~~、~~summarizer 固定首个 `user_id`~~——两项均已解决（分别见未发布「架构重构 Tier1」「按用户解析模型传播收尾」）。

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
- 主工作台 sci-tech 主题重塑（信息架构保留）；移除用户头像功能；新增 SVG 图标库 `api/static/js/icons.js`。
- 前端从 `fastapi_server.py` 内联 HTML 抽离至 `api/static/`（静态化 + no-cache 中间件 + 版本号破缓存）。

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
