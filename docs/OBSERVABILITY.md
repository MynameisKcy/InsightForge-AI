# 可观测性指南

InsightForge AI 的可观测性由三部分组成：**OpenTelemetry 链路追踪**（Jaeger 可视化）、**Agent 决策日志**（JSONL 落盘 + 前端决策卡片）、**Token/成本统计**（SSE 实时推送 + 侧边栏看板）。

---

## 1. OpenTelemetry 链路追踪

### 1.1 开关

OTel 采用 **endpoint 即开关** 的设计（`utils/tracing.py`）：

| 场景 | 行为 |
|------|------|
| 未设置 `OTEL_EXPORTER_OTLP_ENDPOINT` | 完全 NoOp，零开销、无报错日志（本地开发默认） |
| 设置了 endpoint（如 `http://localhost:4318`） | 初始化 OTLP 导出，Span 批量上报 |

Docker 部署（`docker-compose.yml`）自动注入 `http://jaeger:4318`，无需配置即启用。

本地启用：

```bash
# 仓库根起 Jaeger（仅 OTLP + UI 端口）
docker-compose up -d jaeger

# 仓库根 .env 取消注释
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=insightforge

# 重启服务后提问一次，打开 http://localhost:16686（Service=insightforge）
```

### 1.2 Span 字段字典

请求一次 `/api/chat` 后的典型 Span 树（属性在 Jaeger Span 详情中可见）：

| Span | 层级 | 关键属性 |
|------|------|----------|
| `http.request` | 根 | `http.route/user_id/session_id/query_length`；SSE 首事件 `[TRACE]{trace_id}` 可直接在 Jaeger 检索 |
| `agent.reason` | ReactAgent 模型调用 | `duration_ms`、`llm.input_tokens/output_tokens` |
| `tool.{name}` | 每次工具调用 | `tool.name/args(截500)/status/result_summary(截200)/duration_ms` |
| `llm.call` | 子 Agent LLM 调用 | `agent.name`（planner/sql/trend/...）、`llm.prompt_length`、token |
| `planner.plan` | 计划生成 | `planner.step_count/title/reasoning(截200)` |
| `planner.step` | 每个步骤 | `planner.step_index/agent_name/duration_ms`、`status` |
| `sql.generate` | SQL 生成 | `sql.task_length/has_joins/length` |
| `sql.execute` | SQL 执行（含重试） | `sql.query(截500)/rows_returned/attempt/duration_ms` |
| `rag.retrieve` | 检索入口 | `rag.query/k/expanded_query_count/coarse_count/results_count` |
| `rag.rerank` | 精排 | `rag.coarse_count/top_n/score_threshold/kept_count`、`rag.fallback`（降级标记） |
| `memory.recall` | 跨会话记忆 | `memory.user_id/limit/results_count` |

约定：异常统一 `status=ERROR` + `record_exception`（Jaeger 红色高亮，附堆栈）。

### 1.3 上下文传播（实现要点）

`ReactAgent.execute_stream` 在后台线程执行（SSE 心跳机制）。`/api/chat` 的根 Span
经 `contextvars.copy_context()` 传入线程（`fastapi_server.py` 的 `_stream_with_heartbeat`），
保证线程内所有子 Span 正确挂在请求根 Span 之下，链路不断。

### 1.4 常见排障

| 现象 | 原因与处理 |
|------|-----------|
| Jaeger 里看不到链路 | ① endpoint 未设置（NoOp）；② Jaeger 未起；③ 提问后 BatchSpanProcessor 未刷盘——再触发一次或等几秒 |
| OTLP 连接报错刷屏 | endpoint 配错/网络不通；未设置 endpoint 时不会出现该错误 |
| Span 树断裂（子 Span 悬空） | 新代码在线程中创建 Span 前未走 `copy_context` 路径，检查是否绕过了 `_stream_with_heartbeat` |

---

## 2. Agent 决策日志与决策卡片

### 2.1 数据流

```
后台线程（ReactAgent / monitor_tool / PlannerAgent）
  └─ make_decision() → log_decision() 落盘 JSONL
                    → emit_decision() 经 ProgressEmitter → SSE [DECISION:{json}]
                                                      → 前端决策卡片
```

三个数据源：

- **💭 LLM 思考**（`react_agent`）：被内部独白过滤器拦截的推理文本（此前被静默丢弃）
- **🛠 工具调用**（`tool_call`）：monitor_tool 记录的 tool/args/耗时/结果摘要（失败也记录）
- **🧭 规划理由**（`planner`）：PlannerAgent 生成的计划 + 理由（随 `[STEP]` plan 事件下发）

### 2.2 JSONL 格式

文件：`logs/decisions/{YYYY-MM-DD}_{user_id}.jsonl`（按日+用户分片，线程安全写入）

```json
{"timestamp": "2026-09-10T08:30:15+00:00", "user_id": "u_xxx", "session_id": "s_xxx",
 "source": "tool_call", "user_query": "", "reasoning": "",
 "tool_selected": "run_full_analysis", "tool_args": {"query": "分析销售趋势"},
 "execution_time_ms": 3210.5, "result_summary": "前 200 字符摘要..."}
```

排查某用户某天的完整决策序列：`cat logs/decisions/2026-09-10_u_xxx.jsonl | jq .`

---

## 3. Token / 成本统计

- 挂点：`BaseAgent._call_llm`（全部子 Agent）+ `@wrap_model_call` 中间件（ReactAgent），
  读取 LangChain `usage_metadata`；缺失时按「字符数 ÷ 4」估算并标注 `estimated`。
- 计价：`utils/token_counter.py` 的 `PRICE_TABLE_CNY_PER_K`（qwen-turbo/plus/max，
  元/千 token），可用环境变量 `TOKEN_PRICE_INPUT/OUTPUT` 覆盖；未知模型按 qwen-plus 计。
- 展示：每次 LLM 调用累计后推送 SSE `[METRICS:{json}]`，前端侧边栏「会话统计」实时更新；
  删除会话/登出（请求带 session_id）时统计同步清除。
- 局限：进程内存计数（单副本 uvicorn 够用），服务重启清零；RAG 生成链（chain 返回 str）一期未覆盖。

---

## 4. SSE 事件总览（含可观测性新增）

| 事件 | 方向 | 新增/既有 | 用途 |
|------|------|-----------|------|
| `[TRACE]{trace_id}` | 服务端→前端 | 新增 | Jaeger 检索本次请求链路 |
| `[DECISION:{json}]` | 服务端→前端 | 新增 | 决策卡片（思考/工具/规划） |
| `[METRICS:{json}]` | 服务端→前端 | 新增 | Token/成本看板 |
| `[STEP:{json}]` | 服务端→前端 | 既有（plan 事件新增 reasoning 字段） | 步骤清单 |
| `[SESSION]` `[THINKING]` `[CHART:]` `[DONE]` `[ERROR]` `[KEEPALIVE]` | — | 既有 | 不变 |
