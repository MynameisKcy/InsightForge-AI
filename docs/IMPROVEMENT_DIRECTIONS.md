# 可改进方向盘点

> 2026-08-17 基于源码核验 + `docs/SECURITY_AND_LIMITATIONS.md` 架构限制清单 + 在途工作区改动整理。
> 每项含：证据（file:line / 文档章节）、一句话建议、价值/工作量评估。
> 排序：价值优先，同价值取工作量低者。

## 在途改动（未提交，避免冲突先看）

工作区有 5 个文件的未提交修复（+58/-11），主题是**"按用户解析模型"**：网页配置了模型的用户在部分链路上仍被钉死在 `.env` 默认模型（免费额度耗尽 → 403）。

| 文件 | 修复内容 |
| :-- | :-- |
| `agent/tools/agent_tools.py` | `quick_data_insight` 里 `SQLAgent()`/`TrendAgent()` 补传 `user_id` |
| `agents/trend_agent.py` | 构造器接受并透传 `user_id` 给 `BaseAgent` |
| `agents/visualization_agent.py` | `chart_png_path` 改用模块级函数（误当静态方法调用会 AttributeError）；`_resolve_column` 兼容 LLM 给出 list 多列的情况 |
| `api/fastapi_server.py` | `MemoryService` 单例的 `llm_callable` 改为按当前请求用户延迟解析模型（原被首个触发者 `user_id="default"` 钉死） |
| `rag/rag_service.py` | 新增 `_get_chain(user_id)`，`rag_summarize` 按用户解析摘要链 |

这批改动本身就是方向 #2 的部分落地，建议先提交再动相关文件。

---

## 方向清单

### 1. 补齐端点级测试盲区 —— 价值高 / 工作量低

- **证据**：`tests/` 36 个文件中，有 `test_auth` / `test_session_routes` / `test_settings_api` / `test_export_api` / `test_unified_files_api`，但没有针对 `/api/chat` SSE 流、`/api/analysis`、`/api/datasets/*`（upload/delete/schema）、`/api/datasources/reload`、`/api/knowledge/reindex` 的端点级测试（对照 `api/fastapi_server.py:244-918` 的路由清单）。
- **建议**：用 FastAPI `TestClient` + mock agent 流补测；`/api/chat` 的 SSE token 契约（`[STEP:]`/`[CHART:]`/`[DONE]`）改动频繁且要求与前端锁步，最值得先测。
- **参照**：`docs/TESTING.md`（全离线、LLM 100% mock 的既有策略）。

### 2. "按用户解析模型" 传播完整性收尾 —— 价值高 / 工作量低

- **证据**：在途 diff 修了 5 处同类问题；`fastapi_server.py` 新注释自述"后台闲置 finalize 线程读到的是最后一次请求的用户，极端并发下可能用错用户配置"；`docs/SECURITY_AND_LIMITATIONS.md` 也记载"summarizer 的 LLM 固定为首个 user_id"。
- **建议**：全局 grep 仍在无 `user_id` 调用的 `SQLAgent()` / `TrendAgent()` / `get_chat_model()`（tests 除外），统一改为构造器注入或 `contextvars` 解析；`MemoryService` 的 summarizer 改为 per-user factory（`set_summarizer_factory` 机制已存在）。
- **这是正在发生的 bug 类别，不是理论风险。**

### 3. SQL 沙箱资源上限（DoS 防护）—— 价值高 / 工作量低

- **证据**：`docs/SECURITY_AND_LIMITATIONS.md`「安全层面」：AST 只读校验之外**无 CPU/内存/磁盘/行数/超时上限**，只读但昂贵的查询存在 DoS 风险。
- **建议**：`DuckDBManager.execute` 内 `SET memory_limit` / `SET threads` / `SET max_output_buffer_size`，查询超时 + 结果行数截断（如 10k 行），超限返回结构化错误供 `SQLAgent._fix_sql` 回灌。

### 4. SSE 断连的服务端取消 —— 价值高 / 工作量中 ✅ 已落地（2026-08-17，协作式 CancelToken：心跳必检+每 20 chunk 抽检断连 → ReactAgent 流循环/PlannerAgent 步骤边界退出；单次进行中的 LLM 调用仍完成，不抢占）

- **证据**：`docs/SECURITY_AND_LIMITATIONS.md`：客户端中断 SSE 后后台线程仍跑完整任务。`api/fastapi_server.py:340-446` 的 `generate()` 无 `request.is_disconnected()` 检查。
- **建议**：心跳循环里轮询 `is_disconnected()`，置 cancellation token 并传入 `agent.execute_stream` / 流水线，各步骤在步骤边界检查退出；同时可省掉 LLM token 浪费。

### 5. 拆分 `api/fastapi_server.py` 巨石 —— 价值中高 / 工作量中

- **证据**：单文件 1252 行（全仓最大，第二名 608 行），~30 个路由 + SSE 编排 + 认证 cookie + 静态服务 + 数据集管理 + 设置 + 知识库文件全在一起。
- **建议**：按资源拆 `APIRouter`（auth / chat / sessions / datasets / settings / knowledge / export），`fastapi_server.py` 只留 app 工厂与中间件挂载。与方向 1 的测试互补（先有端点测试再拆更安全）。

### 6. RAG 知识库 / 图表知识库按用户隔离 —— 价值中 / 工作量中

- **证据**：`docs/SECURITY_AND_LIMITATIONS.md`：RAG 向量库与图表知识库全局共享，任意用户上传的知识可被他人检索。同文档也给出先例——跨会话记忆的 ChromaDB `memory` collection 已用 shared-collection + `user_id` owner 过滤（`memory/long_term.py`）。
- **建议**：复用记忆层的 owner 过滤模式改造 `rag/vector_store.py`（478 行）与 `chart_knowledge`。

### 7. 流水线并行与阶段级容错 —— 价值中 / 工作量大

- **证据**：`docs/SECURITY_AND_LIMITATIONS.md`「架构层面」：严格顺序执行，`depends_on` 只用于跳过未就绪步骤，不调度并发；无跨代理重试/fallback。
- **建议**：分两步——先加阶段级超时与降级输出（失败阶段产出"本阶段不可用"占位，不阻断报告）；再考虑对 `depends_on` 无交集的步骤用线程池并发。收益取决于真实负载，建议放后。

### 8. DuckDB 实例池上限 —— 价值中 / 工作量低

- **证据**：`docs/SECURITY_AND_LIMITATIONS.md`：`_duckdb_instances` 为无上限 dict，高用户 churn 下内存只增不减。
- **建议**：仿照 Session Memory 已有的 LRU 池做 LRU/TTL 淘汰（表结构可从 `datasources.db` 重建，`_reload_datasets_into_instance` 机制已存在，驱逐成本低）。

### 9. 工程化工具链 —— 价值中 / 工作量低

- **证据**：无 ruff/mypy/pre-commit/CI 配置，无 `pyproject.toml`（依赖仅 `requirements.txt`）；主包内残留 6 处 `print()`（应为 logger）；系统 Python 3.8 vs 项目要求 3.10+，环境漂移只能靠文档约束。
- **建议**：加 `pyproject.toml`（ruff lint+format、依赖声明）+ GitHub Actions 跑 pytest（conda env）+ pre-commit。ruff 顺手清 print/未用导入。

### 10. 日志轮转 —— 价值低 / 工作量极低

- **证据**：`docs/SECURITY_AND_LIMITATIONS.md`：日志按日落盘但无轮转/容量上限。
- **建议**：`TimedRotatingFileHandler(backupCount=...)` 一行改动。

### 11. 演示桩与 dormant 协议清理 —— 价值低 / 工作量极低

- **证据**：`get_weather` / `get_user_id` / `get_user_location` 三个工具为硬编码演示桩；`[AUDIT]` / `[CONTEXT]` SSE token 仅前端解析、后端无产出（dormant）。
- **建议**：桩工具要么删除要么在工具描述里标注"演示用途"防 LLM 误信；dormant token 前后端一起摘除。

### 12. 跨平台（去 Windows-only 假设）—— 价值视部署目标 / 工作量中

- **证据**：PDF 导出注册 Windows 中文字体路径；kaleido/chromium 路径假设 Windows（`AGENTS.md` 已记为 gotcha）；`tmp-kaleido/`、`database/datasources.db.bak` 等本机产物未清理出仓库目录。
- **建议**：仅当目标部署 Linux 时做——字体改为随包分发 + 按平台探测；顺手把本机杂物加入 `.gitignore`。

---

## 建议路线

1. **先提交在途改动**（方向 #2 的第一棒），随后把 #2 收尾扫尾 + #1 补 `/api/chat` SSE 端点测试——都是低工作量、直接消灭正在发生的 403/回归类问题。
2. 安全侧 #3（一行 SET 即可起步）与 #4、#8 同属"防御性小改"，可打包一个小版本。
3. 结构性投资（#5 拆文件、#6 知识库隔离、#7 并行）放在有测试保护之后。
4. #9/#10/#11 可作为新贡献者的入门任务（good first issue）。
