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

---

## 📚 完整文档

- [架构总览](docs/ARCHITECTURE.md) —— 系统架构图与分析流水线数据流
- [核心设计深度剖析](docs/DESIGN_DETAILS.md) —— 子代理编排、SQL 沙箱、多用户隔离、RAG、SSE 等 9 小节
- [项目结构](docs/PROJECT_STRUCTURE.md) —— 完整目录树与文件说明
- [配置说明](docs/CONFIGURATION.md) —— `.env` 与各 YAML 字段详解
- [HTTP API 参考](docs/API_REFERENCE.md) —— 全部接口与鉴权说明
- [安全说明与能力边界](docs/SECURITY_AND_LIMITATIONS.md) —— 安全机制 + 架构 / 功能限制
- [测试](docs/TESTING.md) —— 运行方式与覆盖策略
- [版本更新记录](docs/CHANGELOG.md) —— v0.1 / v0.2 / v0.3

架构决策记录（ADR）见 [docs/adr/](docs/adr/)。
