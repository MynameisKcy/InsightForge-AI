# 架构总览

## 系统架构总览

```
                          浏览器 (index.html 登录页 / app.html 工作台)
                                        │  Bearer Token / Cookie
                                        ▼
                    FastAPI (:8502)  ── require_auth (Depends, 401 拦截)
                          │            ├─ SSE: /api/chat
                          │            ├─ 同步: /api/analysis（ADR-0001 退役，前端不调）
                          │            └─ 会话/设置/文件/数据集/知识库 REST
                          │
            ┌─────────────┴──────────────────────────────┐
            ▼                                              ▼
   ReactAgent (Smart Assistant)                       PlannerAgent (Analysis Pipeline, 工具调用)
   langchain create_agent                          LLM 生成 JSON 计划 -> 顺序派发
   15 个 @tool + 3 个中间件                         _agent_map (7 个子代理)
            │                                              │
            │  run_full_analysis ──────────────────────────┘
            │  document_report ── DocumentReportAgent (独立)
            │  rag_sumarize ── RAG 服务
            ▼
   PlannerAgent 顺序派发 (PipelineContext 串联):
     SQLAgent -> AnalysisAgent(Trend) -> AnalysisAgent(Product) -> AnalysisAgent(Risk)
              -> VisualizationAgent -> ReportAgent -> ExportAgent
        │           │           │            │
        └─── analysis/*.py (纯 pandas/numpy 适配器，无 LLM) ────┘
```

**单入口架构（[ADR-0001](adr/0001-single-entry-analysis-as-tool.md)）**：运行期只有 ReactAgent 一个入口，前端只调 `/api/chat`；分析流水线在 ReAct 循环选中 `run_full_analysis` 工具时同步执行，而非对等模式。`/api/analysis` 已退役（路由仍在、前端不调，与工具共用同一份 per-user 缓存）。术语遵循 [`../CONTEXT.md`](../CONTEXT.md)：Smart Assistant / Analysis Pipeline（非 "mode"）。

- **Smart Assistant（ReactAgent，唯一入口）**：`agent/agent/react_agent.py`，基于 LangGraph `create_agent` 构建（非旧版 `AgentExecutor`）。绑定按 `user_id` 缓存的 LLM、15 个工具、3 个中间件（`monitor_tool` / `log_before_model` / `report_prompt_switch`）；`report_prompt_switch` 在正常模式额外注入跨会话召回（报告模式不注入）。`execute_stream` 是同步生成器，由 FastAPI 放进后台线程跑（见 [§7 SSE 流式与跨线程进度推送](DESIGN_DETAILS.md#7-sse-流式与跨线程进度推送)）。
- **Analysis Pipeline（PlannerAgent，作为工具调用）**：`agent/agents/planner_agent.py`，由 `run_full_analysis` 工具按 `user_id` 取缓存实例并调用 `.run()`。入口处先经 `QueryRewriter` 消解对话指代（见 [§9 Query Rewriting](DESIGN_DETAILS.md#9-query-rewriting两点改写adr-0002)），再用 LLM 生成 JSON 执行计划（`_create_plan`），失败回退关键词默认计划（`_default_plan`），最后按计划顺序调度子代理、结果写入类型化 `PipelineContext`（取代 `prev_results` 字典）。

---

## 数据分析流水线数据流

以"分析近半年利润下降原因并生成报告"为例，`PlannerAgent` 顺序派发（首步 `QueryRewriter` 改写见 [§9①](DESIGN_DETAILS.md#9-query-rewriting两点改写adr-0002)）：

```
0. QueryRewriter    结合短期记忆消解指代 -> 自包含 plan_query（失败回退原 query）
1. SQLAgent          NL->SQL -> DuckDB(沙箱) -> dataframe_json  ──┐
   (_fix_sql: 错误回灌重生成, 最多 3 次尝试)                     │
2. TrendAgent        读 pctx.dataframe_json -> TrendAnalysisAdapter       │
                      (数值时序趋势/MoM 增长率/IQR 异常/移动平均)    │ PipelineContext
3. ProductAgent      读 pctx.dataframe_json -> ProductAnalysisAdapter     │ 串联
                      (分组对比: TOP 项/分布占比/低表现项)        │
4. RiskAgent         读 pctx.dataframe_json -> RiskAnalysisAdapter    │
                      (IQR + Z-score/度量异常/分组异常/低表现)   │
5. VisualizationAgent 读 pctx.dataframe_json + trend/product ->    │
                      LLM 选图型(失败回退启发式) -> Plotly HTML  ─┘
                      每图写入 chart_knowledge SQLite
6. ReportAgent       聚合 pctx 各槽位 -> Jinja2 渲染 Markdown
                      (2 次 LLM: 执行摘要 + 结论)
7. ExportAgent       Markdown -> 导出 (按请求解析格式，未指定回退 md+html)
```

> `analysis/*.py` 四模块（含 `analysis_module.py` 的 Protocol + 适配器）是**纯 pandas/numpy**（零 LLM 调用）；LLM 洞察生成发生在统一 `AnalysisAgent` 包装层，失败时回退到纯计算结果。注意 `trend_analysis.py` 只实现了**环比 MoM**（`pct_change`），未实现同比 YoY。
