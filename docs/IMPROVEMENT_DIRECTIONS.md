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
| 6 | 图表知识库按用户隔离 | `83eafd3` | `rag/chart_knowledge.py` `chart_archive` 加 `owner_user_id` 列；`save_chart` 记录归属（显式参数 > 请求上下文 > default）；四条检索路径 + `clear_old_data` 按"自己 + 公共 system"过滤；旧库打开即幂等迁移存量为 system；`get_chart_insights` 工具与可视化存图链路接入 owner。注：向量库半项（#6 向量库部分）先前已落地。 |
| 8 | DuckDB 实例池上限 | `83eafd3` | `database/duckdb_manager.py` `_duckdb_instances` 改 `OrderedDict` + `_instances_lock`，超 `instance_pool_cap`（`config/agent.yml` `duckdb` 节，默认 50）LRU 驱逐最久未用并 close；被驱逐用户下次访问经 `_reload_datasets_into_instance` 透明重建。 |
| 9 | 工程化工具链 | （本次实现，待提交） | `pyproject.toml`（`requires-python>=3.10` + `[tool.ruff]` E/F/I/UP 忽略 E501/E402 + pytest testpaths，保留 requirements.txt 为安装清单）；ruff 首跑 `--fix --unsafe-fixes` 清 162 处（未用导入/import 顺序/Optional→X\|None 等）+ 5 处探测导入 noqa；`.pre-commit-config.yaml`（ruff+format+通用清理）；`.github/workflows/ci.yml`（Ubuntu+Py3.10 跑 ruff+pytest）；`.gitignore` 补 openspec/.codebuddy/.workbuddy/data/tmp*.csv。 |
| 10 | 日志轮转 | （本次实现，待提交） | `utils/logger_handler.py` `FileHandler` → `TimedRotatingFileHandler(when="midnight", backupCount=30)`；活动文件 `agent.log`，午夜轮转为 `agent.log.YYYY-MM-DD`；`LOG_BACKUP_COUNT=30` 限容量。 |
| 11 | 演示桩与 dormant 协议清理 | （本次实现，待提交） | 删除 3 个硬编码演示桩工具（`get_weather`/`get_user_location`/`get_user_id`，含 `user_ids`/`random` 导入）及 react_agent 工具注册 + 状态映射；`report_prompt` 把"调 `get_user_id`"改为"向用户询问"（防随机 ID 误导）；摘除 `app.js` 中 `[CONTEXT]`/`[AUDIT:]` dormant 解析分支（后端本就无产出）。 |

配套架构收敛（数据库安全接缝 / MemoryService 外观 / 共享 rerank / 模型注入 / 回退契约 / 删死模块 / 包根扁平化）同期完成，见 CHANGELOG「未发布」。

---

## 待办方向

### 7. 流水线并行与阶段级容错 —— 价值中 / 工作量大

- **证据**：`docs/SECURITY_AND_LIMITATIONS.md`「架构层面」：严格顺序执行，`depends_on` 只用于跳过未就绪步骤，不调度并发；无跨代理重试/fallback。
- **建议**：分两步——先加阶段级超时与降级输出（失败阶段产出"本阶段不可用"占位，不阻断报告）；再考虑对 `depends_on` 无交集的步骤用线程池并发。收益取决于真实负载，建议放后。

### 12. 跨平台（去 Windows-only 假设）—— 价值视部署目标 / 工作量中

- **证据**：PDF 导出注册 Windows 中文字体路径；kaleido/chromium 路径假设 Windows（`AGENTS.md` 已记为 gotcha）；`tmp-kaleido/`、`database/datasources.db.bak` 等本机产物未清理出仓库目录。
- **建议**：仅当目标部署 Linux 时做——字体改为随包分发 + 按平台探测；顺手把本机杂物加入 `.gitignore`。

---

## 建议路线

1. ~~#6 知识库按用户隔离~~ / ~~#8 DuckDB 实例池上限~~ 已完成（`83eafd3`）。
2. ~~#9 工程化工具链~~ 已完成（pyproject + ruff + CI + pre-commit，本次实现待提交）。
3. ~~#10 日志轮转~~ 已完成（TimedRotatingFileHandler backupCount=30，本次实现待提交）。
4. ~~#11 演示桩与 dormant 协议清理~~ 已完成（删 3 桩 + 修 prompt + 摘 dormant 前端分支，本次实现待提交）。
5. 结构性投资（#7 并行）放在有测试保护之后（端点测试已就位）。
