# InsightForge AI

> 多智能体协作数据分析平台 —— 用自然语言提问，自动编排 SQL 查询、趋势 / 产品 / 风险分析、交互式图表与多格式报告导出。

面向数据分析师提效与业务人员自助取数：分析师无需手写 SQL 即可完成多表关联与多维分析；业务人员一句话即可取数、出图并生成可导出报告。基于 LangChain + LangGraph，以单一智能客服 Agent 为入口，由 LLM 依据工具描述自主决定直接作答、RAG 问答，或触发完整分析流水线。

---

## 项目亮点

- 单入口 + LLM 自主路由（15 工具动态决策）
- sqlglot AST 级 SQL 只读沙箱，拦截注入与 DDL/DML
- 按用户隔离的内存 DuckDB OLAP，支持跨源 JOIN
- 两阶段 RAG：ChromaDB 粗召 + 精排，自动降级
- 配置热重载：网页改 Key / 模型名，免重启
- SSE 长任务进度推送，15s 心跳保活
- OpenTelemetry 全链路追踪（Jaeger 可视化）+ Agent 决策卡片 + Token/成本看板
- RAG 质量评估（检索命中率 / ragas）与端到端性能基准脚本
- Docker 一键部署（应用 + Jaeger 编排）

---

## 技术栈

| 类别 | 技术 |
|------|------|
| AI 框架 | LangChain 1.3 / LangGraph 1.2 |
| LLM | 通义千问（`ChatTongyi`）/ OpenAI 兼容（`ChatOpenAI`） |
| 向量库与 Rerank | ChromaDB + DashScope Embeddings / `gte-rerank-v2` |
| OLAP | DuckDB（按用户 `:memory:` 实例，`postgres_scan`/`mysql_scan` 跨源） |
| SQL 安全 | sqlglot（AST 只读沙箱 + `safe_ident` 转义） |
| 数据处理 | pandas / numpy |
| 可视化 | Plotly |
| Web 与报告导出 | FastAPI + uvicorn（SSE）/ python-docx · reportlab · Jinja2 |
| 可观测性 | OpenTelemetry 1.27（OTLP → Jaeger）/ 决策 JSONL 日志 / Token 统计 |
| 评估与基准 | ragas（RAG 质量）/ 自研 SSE 计时基准 |

---

## 快速开始

### 环境要求

- Python 3.10+（推荐 conda 隔离依赖）
- 一个 DashScope（通义千问）API Key —— [获取](https://dashscope.console.aliyun.com/apiKey)

### 安装与启动

```bash
git clone https://github.com/MynameisKcy/InsightForge-AI.git
cd InsightForge-AI/agent
conda create -n AnalysisAgent python=3.10 -y && conda activate AnalysisAgent
pip install -r requirements.txt
cp .env.example .env          # 编辑 .env 填入 DASHSCOPE_API_KEY
python -m api.fastapi_server  # 访问 http://localhost:8502
```

注册 → 登录后，在侧边栏上传一份 CSV（如销售明细），待「已就绪」后直接提问：

> 分析各月销售趋势并生成报告

系统将自动走 SQL → 趋势分析 → 可视化 → 报告 → 导出全链路，Plotly 图表内嵌于对话流中。

> 也可启动后在网页「账号设置」面板填写配置——保存即时生效，无需重启；网页配置优先级高于 `.env`，API Key 以 Fernet 加密本地存储、掩码回显。

### 🚀 Docker 一键部署

```bash
git clone https://github.com/MynameisKcy/InsightForge-AI.git
cd InsightForge-AI
cp .env.example .env   # 编辑填入 DASHSCOPE_API_KEY
docker-compose up -d   # 或 ./scripts/deploy.sh（含预检+探活重试）
```

访问：**Demo** http://localhost:8502 · **Jaeger 链路追踪** http://localhost:16686
数据卷挂载 `data/ chroma_db/ logs/`，重启不丢数据。详见 [部署指南](docs/DEPLOYMENT.md)。

---

## 📊 可观测性

集成 OpenTelemetry + Jaeger，Agent 决策链路全追踪（`OTEL_EXPORTER_OTLP_ENDPOINT` 未设置时 NoOp，零开销）：

| 组件 | Span | 追踪内容 |
|------|------|----------|
| HTTP 入口 | `http.request` | 请求根 Span（SSE 全程），首事件下发 trace_id |
| ReactAgent | `agent.reason` / `tool.*` | 每次模型调用（token/耗时）、每次工具调用 |
| 子 Agent | `llm.call` | planner/sql/trend/... 全部 LLM 调用 |
| PlannerAgent | `planner.plan` / `planner.step` | 步骤数/规划理由/每步耗时 |
| SQLAgent | `sql.generate` / `sql.execute` | SQL 语句/返回行数/重试 |
| RAG | `rag.retrieve` / `rag.rerank` | 召回数/rerank 保留数/降级标记 |

链路示例：`http.request → agent.reason → tool.run_full_analysis → planner.plan → planner.step → sql.execute`，异常 Span 红色高亮并带堆栈。

前端同步可视化：对话流中的**决策卡片**（💭 LLM 思考 / 🛠 工具调用 / 🧭 规划理由+耗时）与侧边栏 **Token/成本看板**。决策明细落盘 `agent/logs/decisions/日期_用户.jsonl`。详见 [可观测性指南](docs/OBSERVABILITY.md)。

---

## 🧪 评估与基准

```bash
# RAG 检索命中率（确定性，只花 embed+rerank 费用；实测 20/20 = 100%）
cd agent && python -m pytest tests/rag_eval/test_rag_retrieval_hit.py -m rag_eval -v

# RAG 端到端质量（ragas：faithfulness/answer_relevancy/context_precision）
pip install -r agent/requirements-eval.txt
python -m pytest tests/rag_eval/test_rag_quality.py -m rag_eval -v -s

# 端到端性能基准（P50/P95/P99 + Token/成本；需服务已启动）
python scripts/benchmark.py --base-url http://localhost:8502 --iterations 5
```

评估基于受控语料（`agent/tests/rag_eval/eval_knowledge.md`）+ 20 条事实型问答，
在独立 collection 中跑真实「改写→召回→精排→生成」链路，不污染真实知识库。
详见 [基准说明](docs/BENCHMARK.md)。

---

## 📚 完整文档

- [架构总览](docs/ARCHITECTURE.md) —— 系统架构图与分析流水线数据流
- [核心设计深度剖析](docs/DESIGN_DETAILS.md) —— 子代理编排、SQL 沙箱、多用户隔离、RAG、SSE 等 9 小节
- [项目结构](docs/PROJECT_STRUCTURE.md) —— 完整目录树与文件说明
- [配置说明](docs/CONFIGURATION.md) —— `.env` 与各 YAML 字段详解
- [HTTP API 参考](docs/API_REFERENCE.md) —— 全部接口与鉴权说明
- [安全说明与能力边界](docs/SECURITY_AND_LIMITATIONS.md) —— 安全机制 + 架构 / 功能限制
- [测试](docs/TESTING.md) —— 运行方式与覆盖策略
- [可观测性指南](docs/OBSERVABILITY.md) —— OTel 开关、Span 字段字典、Jaeger 使用与排障
- [部署指南](docs/DEPLOYMENT.md) —— 本地 / Docker / 阿里云 ECS 三种方式与环境变量
- [性能基准](docs/BENCHMARK.md) —— 基准方法学、结果解读与多轮对比
- [版本更新记录](docs/CHANGELOG.md) —— v0.1 / v0.2 / v0.3

架构决策记录（ADR）见 [docs/adr/](docs/adr/)。
