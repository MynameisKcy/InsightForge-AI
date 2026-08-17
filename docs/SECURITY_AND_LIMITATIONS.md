# 安全说明与能力边界

## 安全机制

- `.env` 已在 `.gitignore` 中忽略且**未被 git 跟踪**（`.gitignore:2-3`），内含真实 `DASHSCOPE_API_KEY` 与 `INSIGHTFORGE_SETTINGS_KEY`。请勿提交；若历史上曾误提交，应立即在 DashScope 控制台轮换 Key。
- 用户密码以 `bcrypt` 哈希存储（兼容旧 SHA-256 并在下次登录惰性升级）；令牌为 `secrets.token_hex(16)` 随机串、24h 过期、登出 / 改密 / 改昵称即清进程内缓存。
- 用户 API Key 以 Fernet 对称加密存于 `user_settings.db`，主密钥在 `INSIGHTFORGE_SETTINGS_KEY`；任何能读 `.env` 者可解密，请妥善保护该文件与运行环境。
- 会话端点均做 owner 校验并返回 404 防枚举；数据集 / 文件操作按 `owner_user_id` 隔离并有路径穿越防护。
- 生产部署建议：在反代层叠加超时 / 限流 / 请求体大小上限，并为只读 SQL 查询增加资源配额（补齐沙箱未覆盖的 DoS 面）。

## 架构 / 功能限制

为避免误用，以下为经源码核验的能力边界（非缺陷清单，而是"它是什么 / 不是什么"）：

### 架构层面

- 子代理为**静态注册**（`_agent_map` 硬编码），无动态插件 / 注册表 / 磁盘加载机制；新增代理需改 `planner_agent.py` + `agents/__init__.py`；但新增**分析类型**只需加一个 `AnalysisModule` 适配器（`analysis/analysis_module.py`）。
- 流水线**严格顺序执行**，无并行；`depends_on` 仅用于"跳过未就绪步骤"，不调度并发。
- 无跨代理重试 / fallback；仅 `SQLAgent._fix_sql` 内部错误回灌重生成（最多 3 次）。

### 安全层面

- "沙箱"是 **SQL 语句级 AST 校验**，非进程 / 容器 / 虚拟化隔离。查询通道带连接级资源上限（`config/agent.yml` `duckdb` 节：`memory_limit` 1GB / `threads` 2 经连接配置应用，`max_result_rows` 10000 超限报错喂 `_fix_sql` 自愈，`query_timeout_seconds` 30 由 watchdog `conn.interrupt()` 中断），可缓解但**非硬隔离**——磁盘占用（内存超限落盘临时文件）与进程级总量仍无上限。
- RAG 向量库与图表知识库为**全局共享**，无按用户隔离（任意用户的上传知识可被他人检索到）；但**跨会话记忆召回**的 ChromaDB `memory` collection 采用 shared-collection + `user_id` owner 过滤（`include_public=False`），按用户隔离。
- 无 CSRF token、无限流 / 登录防爆破、无 CORS 配置（同源）、无 CSP；图表 iframe 无 `sandbox` 属性；前端 XSS 防护为自研 `escapeHtml` + URL 协议白名单（未用 DOMPurify）。
- 客户端中断 SSE 后有**协作式服务端取消**（`utils/cancel_token.py`）：主协程检测到断连（心跳必检 / 每 20 chunk 抽检 `request.is_disconnected()`）即置 `CancelToken`，`ReactAgent` 流循环与 `PlannerAgent` 步骤边界轮询退出，`run_full_analysis` 工具不吞取消异常。**不抢占**：单次进行中的 LLM 调用仍会完成（无安全中断 API），取消发生在下一个边界。

### 功能层面

- **无多模态**：仅文本理解，PDF 不做 OCR / 图像解析。
- **无模型 fallback 链**：单模型 per user，`ChatTongyi`/`ChatOpenAI` 为 provider 选择而非故障切换。
- 导出 4 种格式（Word / MD / PDF / HTML）均已实现；`ExportAgent` 按用户请求文本解析格式（`_resolve_export_formats`），未指定时回退 `md+html`，也可经 `POST /api/report/export` 端点直接指定 `format` 下载。PDF 已注册中文字体 + 渲染表格 + 图片占位（不再整丢表格）。
- `DocumentReportAgent` **不走 RAG**，直接把原文截断至 8000 字塞给 LLM。
- 记忆为两级（[ADR-0003](adr/0003-two-tier-memory-session-and-user-scoped.md)）：Session Memory 按 `session_id` 隔离 + DB 回灌 + 跨会话召回注入系统提示（详见 DESIGN_DETAILS §4）——原"摘要写入不回灌 / 短期按 user_id 索引跨会话串扰 / summarizer 的 LLM 固定为首个 `user_id`"三项限制**已解决**（`MemoryService` 现以 `llm_factory(user_id)` + per-user summarizer 工厂按请求用户解析模型）；`MemoryService` 仍为进程级单例，但不再持有用户态。
- `get_weather` / `get_user_id` / `get_user_location` 三个工具为**演示桩**（返回硬编码 / 随机值）。
- 审计通道 `[AUDIT]` / `[CONTEXT]` 仅有前端解析、无后端产出，处于 dormant 状态。
- `_duckdb_instances` 为无上限 dict，高用户 churn 下内存只增不减。
- 日志按日落盘但**无轮转 / 容量上限**。
