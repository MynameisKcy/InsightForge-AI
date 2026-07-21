# InsightForge 配置管理 + 文件管理 + 报告图表改造设计

- 日期：2026-07-21
- 来源需求：`改进.txt`（桌面）
- 状态：已与用户确认关键决策，待实现

## 1. 背景与定位

`改进.txt` 把系统定位为「单用户本地 RAG 应用」，但现有代码 InsightForge AI 是多用户、带登录、刚完成安全加固（最近 5 个 commit 为 security round）的平台。

**确认定位**：默认单用户、保留登录但可选。所有新表/新 API/新文件都绑定 `owner_user_id`，与已加固的多用户隔离一致；默认单用户只是「只有一个 user 在用」的退化情形。不回退已交付的安全工作。

## 2. 关键决策（用户已确认）

1. **热重载**：`factory.py` 改 getter 模式 + 版本号缓存，真热重载（方案 A）。
2. **配置存储**：后端 SQLite 新表，绑定 user_id；前端只做表单。API Key 用 Fernet 对称加密存储，返回前端时掩码。
3. **Excel/CSV**：分轨制——表格进 DuckDB、文本进 Chroma；文件管理页统一展示两类。
4. **文本报告**：新增专用 `DocumentReportAgent`（摘要+要点+可选 Q&A，复用 ExportAgent 导出）。
5. **混合调度**：ReactAgent + LLM 自动判断选文件/选管道，不写硬编码路由规则。
6. **设置页**：侧边栏折叠面板（与数据集/知识库同级）。
7. **向量库连接**：ChromaDB 本地为主，远程 host/port/tenant 字段可选预留，不可用时禁用。
8. **配置优先级**：用户页面配置 > `.env` 环境变量 > YAML 默认。
9. **上传进度**：multipart 原生进度 + 解析状态轮询。
10. **未配置提示**：用户登录后若 `user_settings` 无记录，前端弹提示横幅 + 侧边栏红点，引导填写；不阻塞，getter 回退默认值保证不崩。

## 3. 架构

```
                       ┌─────────────────────────────────────────┐
                       │  FastAPI (fastapi_server.py)            │
                       │  侧边栏新增: 账号设置面板                  │
   浏览器 ──SSE──▶     │  侧边栏改造: 统一文件管理面板(文本类+表格类) │
                       └───────────┬─────────────────────────────┘
                                   │
        ┌──────────────────────────┼───────────────────────────┐
        ▼                          ▼                           ▼
  ① 配置管理                  ② 文件管理                  ③ 报告/图表
  ─────────────              ─────────────              ─────────────
  config.db (新表)           文本类 → Chroma(现有)        DocumentReportAgent(新增)
  user_id 绑定               表格类 → DuckDB(现有)        专用文本报告:摘要+要点+Q&A
  /api/settings/* (新)       统一文件列表API             复用 ExportAgent 导出
  factory.get_*() 热重载     /api/knowledge/* 改造       ReactAgent 加 list_user_files
  前端表单 + 掩码            两类同展示+状态              @tool, LLM 自动选文件/管道
```

原则：复用优先、分轨不混流、热重载局部化、绑定 user_id、本地优先。

## 4. 组件清单

### ① 配置管理

**新增**
- `database/user_settings_db.py` — `UserSettingsDB`，表 `user_settings(user_id PK, llm_api_key_enc, llm_model_name, embedding_model_name, vector_db_host, vector_db_port, vector_db_collection, vector_db_tenant, local_db_conn, updated_at)`。方法 `get/upsert/has`。API Key 用 Fernet 加密（密钥派生自环境变量 `INSIGHTFORGE_SETTINGS_KEY`，缺失则生成并写 `.env`）。
- API：
  - `GET /api/settings` — 返回当前用户配置，API Key 掩码，未配置返回 `null`
  - `POST /api/settings` — 保存，bump 模型版本号触发热重载
  - `GET /api/settings/status` — `{configured: bool}`，登录后前端先查决定是否弹提示
- 前端：侧边栏「账号设置」折叠面板，分组（LLM/向量/数据库），API Key 默认掩码、点「编辑」才明文输入；保存后 toast「配置已生效」。

**改造**
- `model/factory.py` — `get_chat_model()/get_embed_model()` getter + 版本号缓存（方案 A）；`reload_model_config(user_id)` 保存后调用。`ChatModelFactory/EmbeddingsFactory` 按优先级取值：用户配置 > `.env` > YAML。
- 所有 `from model.factory import chat_model` 处改为调 getter：`react_agent.py`、`agents/base.py`、`rag/vector_store.py`、`rag/rag_service.py`、`tools/agent_tools.py`。

### ② 文件管理

**改造（复用为主）**
- `POST /api/knowledge/upload` — 已支持多文件 PDF/Word/TXT/MD。补解析状态字段。
- `GET /api/knowledge/files` — 文本类列表 + 状态。
- 新增 `GET /api/files` — 合并文本类（Chroma）与表格类（DuckDB）返回统一列表，前端文件管理页一次渲染。
- 表格类上传仍走 `POST /api/datasets/upload` → DuckDB，不进向量库（分轨制）。
- 删除：文本类 `DELETE /api/knowledge/files/{filename}`（含向量库移除）；表格类 `DELETE /api/datasets/{name}`（含 DuckDB 表删除）。
- 大文件：上传前检查大小，PDF/Excel >50MB 返回预估处理时间或拒绝，CSV 沿用 100MB 上限。

### ③ 报告与图表

**新增**
- `agents/document_report_agent.py` — `DocumentReportAgent(BaseAgent)`，输入文本文件路径/内容，输出结构化 Markdown：摘要 + 关键词/要点 + 可选 Q&A。复用 `BaseAgent._call_llm()`。可选导出复用 `ExportAgent`。
- ReactAgent 新增 `@tool list_user_files` — 返回当前用户文件清单（类型/表名/状态）。
- ReactAgent 路由：靠 LLM 工具调用，用户自然语言指令 → LLM 调 `list_user_files` → 选文件 → 表格类触发 `run_full_analysis`，文本类触发 `document_report` 工具。

**复用不动**：`VisualizationAgent`、`ReportAgent`、`ExportAgent`。

## 5. 数据流

**流① 首次配置与热重载**
```
登录 → GET /api/settings/status → {configured:false}
  → 前端弹提示横幅 + 侧边栏红点
  → 用户填表单 → POST /api/settings
  → UserSettingsDB.upsert (API Key Fernet 加密)
  → factory.reload_model_config(user_id) bump 版本号
  → 下次 get_chat_model()/get_embed_model() 检测版本变化 → 重建实例
  → 返回「配置已生效」, 无需重启
```
优先级：用户配置 > `.env` > YAML 默认。未配置 getter 回退默认。

**流② 文件上传（分轨）**
```
拖拽多文件 → 前端按扩展名分流
  ├─ .pdf/.docx/.txt/.md → POST /api/knowledge/upload → Chroma
  └─ .csv/.xlsx/.xls      → POST /api/datasets/upload → DuckDB
上传中: multipart 原生进度条
上传后: 轮询 GET /api/files?status=pending → 处理中/已完成/失败
```

**流③ 对话触发报告/图表（LLM 自动路由）**
```
用户: "根据上传的销售数据生成季度报告"
  → POST /api/chat (SSE) → ReactAgent
  → LLM 调 @tool list_user_files → 文件清单
  → LLM 判断: 销售数据=表格类 → run_full_analysis
      → SQLAgent → Trend/Product/Risk → VisualizationAgent(Plotly) → ReportAgent
      → SSE [CHART:url] + 报告文本
  → (文本文件如合同.pdf) LLM 调 document_report 工具
      → DocumentReportAgent → 摘要+要点+Q&A Markdown
      → 可选 ExportAgent 导出 docx/pdf/html
  → [DONE]
```

**流④ 删除**
```
前端删除项 → 按类型分流
  ├─ 文本类 → DELETE /api/knowledge/files/{name} → Chroma.delete_by_source + 删磁盘
  └─ 表格类 → DELETE /api/datasets/{name} → DuckDB.drop_table + 删文件 + 删元数据
```

## 6. 错误处理

沿用「SSE `[ERROR:text]` + 前端 toast」，所有新流程不白屏、不崩溃、给明确文案。

| 场景 | 处理 |
|---|---|
| 未配置 LLM 又调用对话/报告 | getter 回退默认；默认也缺 → `[ERROR:尚未配置 LLM，请在账号设置中填写]` + 弹设置面板 |
| API Key 无效/模型名错 | DashScope 401/400 → ReactAgent 捕获 → `[ERROR:LLM 调用失败：<原因>，请检查账号设置]`；保留配置不回滚 |
| 热重载期间并发请求 | getter 锁保护版本号比对+重建；老请求用旧实例，新请求等重建完拿新实例 |
| 文件格式不支持 | 400 + 「不支持的格式：.xxx，仅支持 PDF/Word/TXT/Excel/CSV」 |
| 文件过大 | >50MB 返回预估处理时间或拒绝；CSV 沿用 100MB 上限 |
| 文件解析失败 | PDF 损坏/Excel 加密 → 状态「失败」+ 原因 + 重试按钮；不影响其他文件 |
| Excel/CSV 进 DuckDB 失败 | 列名非法/编码错 → 状态「失败」+ 原因；沿用 `safe_ident()` |
| 文本向量化失败 | Chroma 异常 → 状态「失败」；已部分入库的 chunks 按 file_md5 回滚 |
| DocumentReportAgent 失败 | LLM 超时/空 → 重试 1 次（沿用 BaseAgent）→ 仍失败 → `[ERROR:报告生成失败]`，已生成部分仍返回 |
| LLM 选错文件 | 工具返回「文件不存在，可用：<列表>」→ LLM 自纠正（ReAct 天然） |
| 删除向量库移除失败 | 先删元数据+磁盘再删向量；向量删除失败记日志不阻塞，标记「部分删除」+ 提示 reindex |
| 并发上传同名文件 | 沿用现有 datasources_db/md5 去重行为 |

兜底：所有新 API 用 FastAPI 异常处理包裹，未预期异常返回 500 + 通用文案，不透 traceback；前端 fetch 失败显示 toast 不白屏。

## 7. 测试

沿用 `pytest`/`unittest` 双轨，`tests/` 目录，LLM 调用一律 mock，离线可跑。

| 层 | 测试 |
|---|---|
| 配置管理 | `test_user_settings_db`（upsert/get/has、API Key 加密往返、掩码）；`test_factory_getter`（版本号热重载、旧实例引用不变、并发无泄露）；`test_settings_api`（掩码、未配置 null、status 真值）；`test_config_priority`（用户配置 > .env > YAML 三级回退） |
| 文件管理 | `test_file_routing`（扩展名分流）；`test_unified_file_list`（`/api/files` 合并两类+状态）；`test_upload_validation`（白名单、大小阈值文案、解析失败状态）；`test_delete_sync`（两类删除联动） |
| 报告/图表 | `test_document_report_agent`（文本→含摘要/要点/Q&A 的 Markdown）；`test_list_user_files_tool`（返回清单含类型/表名）；`test_hybrid_routing`（桩 LLM 断言「销售数据」走表格管道、「总结合同」走文本管道） |
| 错误处理 | `test_error_paths`（未配置、LLM 401、超大文件、损坏 PDF） |
| 安全 | `test_user_isolation`（A 的配置/文件对 B 不可见）；`test_sql_injection`（沿用 safe_ident/sqlglot 沙箱） |

## 8. 非功能性

- 网络：除 LLM/Embedding 调 DashScope 外，配置存储、文件解析、图表生成、报告导出全部本地完成。
- 性能：大文件（PDF/Excel >50MB）给出处理时间预估或限制提示。
- 错误：所有操作友好提示，避免白屏或崩溃。
