# InsightForge AI — Multi-Agent Data Analysis System

> 基于 LangChain + LangGraph 的多智能体协作数据分析平台，支持自然语言交互、自动化数据探查、趋势/产品/风险分析、交互式图表生成、多格式报告导出。

---

## 目录

- [概述](#概述)
- [系统架构](#系统架构)
- [核心功能](#核心功能)
- [Agent 协作流程](#agent-协作流程)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [API 接口](#api-接口)
- [界面预览](#界面预览)

---

## 概述

InsightForge AI 是一个多智能体协作的数据分析系统。用户通过自然语言描述分析需求，系统自动规划任务、查询数据、执行多维度分析（趋势、产品、风险）、生成交互式图表，并输出结构化的商业分析报告。

系统设计为 **7 个专业 Agent** 协作完成分析工作流，每个 Agent 各司其职：从任务规划、SQL 查询、多维度分析到图表生成、报告撰写、多格式导出，形成完整的分析链路。

---

## 系统架构

```
User (FastAPI / Streamlit)
  │
  ▼
ReactAgent (智能客服入口 — LangGraph Agent)
  │  自动判断需求类型，调度工具
  │
  ├── rag_sumarize       → RAG 知识库问答（ChromaDB）
  ├── run_full_analysis   → 触发完整分析流程
  ├── quick_data_insight  → 快速数据分析
  ├── get_data_overview   → 数据概况探查
  ├── get_chart_insights  → 图表知识库检索
  └── get_external_data   → 外部数据查询

       │ (run_full_analysis 内部)
       ▼
Planner Agent (任务规划 — 编排调度)
  │
  ├── SQL Agent          → 自然语言转 SQL → DuckDB 执行 → DataFrame
  ├── Trend Agent        → 趋势分析（增长率、异常检测、同比环比）
  ├── Product Agent      → 产品分析（TOP 排名、类别贡献、利润分析）
  ├── Risk Agent         → 风险分析（异常检测、区域风险、亏损产品）
  ├── Visualization Agent → 图表生成（Plotly：折线/柱状/饼图/热力/散点）
  ├── Report Agent       → 报告生成（Jinja2 模板 → Markdown）
  └── Export Agent       → 多格式导出（Markdown / Word / PDF / HTML）
```

---

## 核心功能

### 数据分析

| 功能 | 描述 |
|------|------|
| SQL 智能查询 | 自然语言自动转 SQL，DuckDB 执行，支持 CSV 数据源 |
| 趋势分析 | 月度/年度趋势、增长率、异常检测、时间序列分析 |
| 产品分析 | TOP 产品排名、类别贡献、高销量低利润产品识别 |
| 风险分析 | IQR + Z-score 异常检测、区域风险、利润异常 |
| 图表生成 | Plotly 交互式图表：折线图、柱状图、饼图、热力图、散点图 |
| 报告生成 | Jinja2 模板引擎，自动生成结构化 Markdown 分析报告 |
| 多格式导出 | 支持 Markdown / Word (docx) / PDF / HTML 导出 |

### 智能客服

| 功能 | 描述 |
|------|------|
| RAG 知识库 | ChromaDB 向量检索 + LLM 总结，支持 txt/pdf 知识库 |
| 图表知识库 | SQLite 存储历史图表元数据，支持检索和对比分析 |
| 用户报告 | 外部数据关联，自动生成个性化使用报告 |
| 会话管理 | 多会话切换、对话历史持久化（短期 + 长期记忆） |
| 流式输出 | SSE 流式响应，思考状态实时反馈、打字机效果 |
| 用户认证 | 注册/登录系统，基于 Token 的身份验证 |

---

## Agent 协作流程

以用户提问"分析近半年利润下降原因并生成报告"为例：

```
1. Planner Agent
   └── 理解需求 → 拆解为 SQL + Trend + Product + Risk + Viz + Report + Export

2. SQL Agent
   └── 自然语言 → SQL → DuckDB → DataFrame

3. Trend Agent
   └── 月度利润趋势、增长率、异常月份检测

4. Product Agent
   └── TOP/低利润产品、类别贡献分析

5. Risk Agent
   └── IQR + Z-score 异常检测、区域风险识别

6. Visualization Agent
   └── 自动选择图表类型 → 生成 Plotly 交互式图表

7. Report Agent
   └── 整合所有分析 → Jinja2 渲染 → Markdown 报告

8. Export Agent
   └── Markdown → Word / PDF / HTML 多格式导出
```

---

## 技术栈

| 层级 | 技术 |
|------|------|
| **AI 框架** | LangChain, LangGraph |
| **LLM** | 通义千问 (Qwen3-Max), DashScope Embeddings, DashScope Rerank (gte-rerank-v2) |
| **向量数据库** | ChromaDB |
| **中文分词** | jieba（搜索模式，提升图表知识库中文召回） |
| **数据查询** | DuckDB (OLAP 嵌入式数据库，按用户隔离的 :memory: 实例) |
| **数据分析** | Pandas, NumPy |
| **可视化** | Plotly (交互式图表) |
| **Web 框架** | FastAPI (SSE 流式), Streamlit |
| **报告导出** | python-docx (Word), ReportLab (PDF), Jinja2 (模板) |
| **记忆系统** | 短期记忆（会话上下文）+ 长期记忆（SQLite 持久化） |
| **数据库** | SQLite（用户认证、图表知识库、对话历史） |
| **安全** | bcrypt 密码哈希、secrets.compare_digest 防时序攻击、SQL 只读白名单沙箱 |
| **语言** | Python 3.10+ |

### v0.1 关键特性

- **多用户并发隔离**：DuckDB 按 `user_id` 缓存独立内存实例，PlannerAgent 请求级状态下沉，多用户并发分析不串数据
- **NL→SQL 安全沙箱**：白名单只允许 SELECT/WITH/SHOW/DESCRIBE 等只读语句，禁 DROP/CREATE/INSERT 等写操作；LLM 生成错误时自动回灌错误信息并重试纠错
- **RAG 检索升级**：chunk 500 + 粗召回 k=15 + DashScope rerank 精排 top_n=3（阈值 0.3）+ jieba 中文分词，检索相关性显著提升
- **知识库运行时管理**：Web 界面上传 / 列表 / 删除 / 全量重建索引，支持 txt / pdf / docx / md 四种格式，无需手动跑脚本入库
- **配置统一**：`.env` 作为模型 Key 与名称的单一真相源，YAML 仅作 fallback

---

## 项目结构

```
agent/
├── api/
│   └── fastapi_server.py     # FastAPI Web 服务（SSE 流式 + 用户认证）
├── agents/
│   ├── planner_agent.py      # Planner Agent — 任务规划与编排
│   ├── sql_agent.py          # SQL Agent — 自然语言转 SQL
│   ├── trend_agent.py        # Trend Agent — 趋势分析
│   ├── product_agent.py      # Product Agent — 产品分析
│   ├── risk_agent.py         # Risk Agent — 风险分析
│   ├── visualization_agent.py # Visualization Agent — 图表生成
│   ├── report_agent.py       # Report Agent — 报告生成
│   └── export_agent.py       # Export Agent — 多格式导出
├── agent/
│   ├── react_agent.py        # ReactAgent — LangGraph 智能客服入口
│   └── tools/
│       ├── agent_tools.py    # 工具函数集合
│       └── middleware.py     # LangGraph 中间件
├── analysis/
│   ├── trend_analysis.py     # 趋势分析算法
│   └── product_analysis.py   # 产品分析算法
├── visualization/
│   └── charts.py             # Plotly 图表生成器
├── rag/
│   ├── rag_service.py        # RAG 检索增强生成
│   ├── vector_store.py       # ChromaDB 向量存储
│   └── chart_knowledge.py    # 图表知识库
├── memory/
│   ├── short_term.py         # 短期记忆（会话上下文）
│   ├── long_term.py          # 长期记忆（SQLite 持久化）
│   └── summarizer.py         # 对话摘要压缩
├── database/
│   ├── duckdb_manager.py     # DuckDB 数据管理
│   ├── schema_loader.py      # Schema 解析
│   ├── data_resolver.py      # 数据集自动检测
│   └── user_db.py            # 用户认证数据库
├── model/
│   └── factory.py            # LLM 模型工厂（读 .env）
├── utils/
│   ├── request_context.py    # 请求级 contextvars（user_id 透传）
│   ├── file_handler.py       # 知识库文件加载器（txt/pdf/docx/md）
│   └── ...                   # 其他工具模块
├── config/                   # 配置文件 (YAML)
├── prompts/                  # Prompt 模板
├── templates/                # Jinja2 报告模板
├── reports/                  # 生成的报告和图表（运行时，已 gitignore）
├── data/                     # 数据文件（含知识库源文件）
├── .env                      # 环境变量（DASHSCOPE_API_KEY、模型名，已 gitignore）
├── requirements.txt          # 依赖清单
├── app.py                    # Streamlit 界面入口
├── goal.md                   # 项目目标文档
└── README.md                 # 本文件
```

---

## 快速开始

### 环境要求

- Python 3.10+
- Conda 环境（推荐）

### 安装

```bash
# 克隆项目
git clone <repository-url>
cd agent

# 创建 conda 虚拟环境（Python 3.10）
conda create -n AnalysisAgent python=3.10
conda activate AnalysisAgent

# 安装依赖
pip install -r requirements.txt
```

### 配置

**1. 在 `agent/` 目录下创建 `.env`**（模型 Key 与名称的单一真相源，**不会提交到 Git**）：

```dotenv
# DashScope API Key（通义千问 + 向量 + rerank 共用）
DASHSCOPE_API_KEY=sk-your-dashscope-api-key

# 大语言模型名
CHAT_MODEL_NAME=qwen3-max

# 文本嵌入模型名
EMBEDDING_MODEL_NAME=text-embedding-v4
```

> `.env` 已在 `.gitignore` 中忽略。`config/rag.yml` 中的模型名为 fallback，实际以 `.env` 为准。

**2. RAG 检索参数**（`config/rag.yml`，已预置推荐值，通常无需改动）：

```yaml
rerank_model: gte-rerank-v2      # DashScope rerank 模型（gte-rerank 对部分 Key 返回 403，须用 v2）
retrieve_k: 15                    # 粗召回候选数
rerank_top_n: 3                  # rerank 后保留的文档数
rerank_score_threshold: 0.3       # rerank 分数阈值，低于此分丢弃
```

**3. 向量库参数**（`config/chroma.yml`）：`chunk_size: 500`、`chunk_overlap: 50`、`allowed_knowledge_file_type: ["txt","pdf","docx","md"]`。

**4. 外部数据路径**（`config/agent.yml`）：

```yaml
external_data_path: data/external/records.csv
```

> ⚠️ 改动 `chunk_size` 后需清库重灌：启动服务后在前端「📚 知识库 → ⟳ 全量重建索引」，或调用 `POST /api/knowledge/reindex`。

### 启动

**方式一：FastAPI Web 服务（推荐）**

```bash
python -m api.fastapi_server
# 访问 http://localhost:8502
# 先注册账号 → 登录 → 开始分析
```

**方式二：Streamlit 界面**

```bash
streamlit run app.py
# 访问 http://localhost:8501
```

### 使用示例

1. "帮我分析各月销售趋势并生成报告"
2. "哪个产品类别利润最高？"
3. "最近几个月销售额是否在下降？"
4. "对比各区域的表现，找出异常"
5. "给我生成我的月度使用报告"
### 运行效果截图
<img src="./images/show_image1.png" width="500">
<img src="./images/show_image2.png" width="500">
<img src="./images/show_image3.png" width="500">
---

## 配置说明

| 配置文件 | 说明 |
|----------|------|
| `.env` | **真相源**：DASHSCOPE_API_KEY、CHAT_MODEL_NAME、EMBEDDING_MODEL_NAME（已 gitignore） |
| `config/rag.yml` | rerank 模型、粗召回/精排参数、阈值（fallback 模型名） |
| `config/chroma.yml` | ChromaDB 配置（分块大小、检索数量、允许的文件类型） |
| `config/agent.yml` | 外部数据路径配置 |
| `config/prompts.yml` | Prompt 模板路径配置 |

---

## API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 登录页面 |
| `/app` | GET | 主应用页面 |
| `/api/register` | POST | 用户注册（密码 bcrypt 哈希，≥8 位） |
| `/api/login` | POST | 用户登录（返回 token） |
| `/api/logout` | POST | 用户登出 |
| `/api/chat` | POST | 智能客服（SSE 流式响应，按 user_id 隔离） |
| `/api/analysis` | POST | 数据分析（同步 JSON，按 user_id 隔离） |
| `/api/sessions` | GET | 获取会话列表 |
| `/api/sessions/{id}` | GET | 获取会话详情 |
| `/api/knowledge/files` | GET | 列出知识库文件（含大小/md5/是否已入库） |
| `/api/knowledge/upload` | POST | 上传文件并增量入库（multipart，支持 txt/pdf/docx/md） |
| `/api/knowledge/files/{filename}` | DELETE | 删除文件及其向量分片 |
| `/api/knowledge/reindex` | POST | 清库全量重建索引（需 `confirm=true`） |
| `/api/knowledge/stats` | GET | 知识库统计（分片数/来源数/维度） |
| `/api/health` | GET | 健康检查 |

---

## 界面预览

- **登录页**：用户注册/登录（bcrypt 加密、密码强度校验）
- **主界面**：左侧会话列表 + 知识库管理面板 + 右侧对话区
- **知识库面板**：上传/删除/全量重建，文件状态徽章（已入库/待入库）
- **思考状态**：spinner 动画 + 当前执行步骤提示
- **流式输出**：分析结论逐句呈现
- **图表嵌入**：Plotly 交互式图表内嵌展示
- **报告下载**：支持 Markdown / Word / PDF / HTML 下载

---

*本项目由 AI Data Analyst Multi-Agent System 驱动*
