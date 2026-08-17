# 可改进方向盘点

> 2026-08-17 基于源码核验 + `docs/SECURITY_AND_LIMITATIONS.md` 架构限制清单整理，同日完成 #1-#5。
> 每项含：证据（file:line / 文档章节）、一句话建议、价值/工作量评估。排序：价值优先，同价值取工作量低者。
> 完成状态以 git log 为唯一真相。

## 已完成（仅存目，详情见 CHANGELOG「未发布」与对应 commit）

| # | 方向 | 落地 commit | 摘要 |
|---|------|------------|------|
| 1 | 端点级测试盲区 | `d0496fb` | `tests/conftest.py` 共享 fixtures + chat SSE / analysis / datasets / reload / reindex 五组端点测试（+31 用例） |
| 2 | 按用户解析模型传播收尾 | `6e3d051` `cb6bf22` `3583567` | `quick_data_insight`/TrendAgent/ExportAgent/DocumentReportAgent 补传 `user_id`；`MemoryService` 改 `llm_factory(user_id)` + summarizer per-user 工厂（消竞态）；RAG 摘要链 `_get_chain(user_id)` |
| 3 | SQL 沙箱资源上限 | `d15d3c8` | `config/agent.yml` `duckdb` 节（1GB 内存 / 2 线程 / 1 万行 / 30s 超时），连接配置 + 行上限报错喂 `_fix_sql` 自愈 + `conn.interrupt()` watchdog |
| 4 | SSE 断连服务端取消 | `ebb6f61` | `utils/cancel_token.py` 协作式取消：心跳必检 + 每 20 chunk 抽检断连，ReactAgent 流循环 / PlannerAgent 步骤边界退出（不抢占单次 LLM 调用） |
| 5 | 拆分 fastapi_server 巨石 | `0ad20ce` | 1252 行 → 组装根（~110 行）+ `api/deps.py`（服务接缝）+ `api/sse.py` + `api/serialization.py` + `api/routes/` 八模块 |

配套架构收敛（数据库安全接缝 / MemoryService 外观 / 共享 rerank / 模型注入 / 回退契约 / 删死模块 / 包根扁平化）同期完成，见 CHANGELOG「未发布」。

---

## 待办方向

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

- **证据**：无 ruff/mypy/pre-commit/CI 配置，无 `pyproject.toml`（依赖仅 `requirements.txt`）；系统 Python 3.8 vs 项目要求 3.10+，环境漂移只能靠文档约束。
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

1. **#6 知识库按用户隔离**为下一优先（安全侧，记忆层已有可复用先例）；#8 DuckDB 实例池上限同为防御性小改，可打包一个小版本。
2. 结构性投资（#7 并行）放在有测试保护之后（端点测试已就位）。
3. #9/#10/#11 可作为新贡献者的入门任务（good first issue）。
