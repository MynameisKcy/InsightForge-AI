# 项目结构

```
InsightForge-AI/
├── README.md                      # 项目说明（精简版 + 完整文档索引）
├── CLAUDE.md                      # 给 AI 助手的项目指引
├── CONTEXT.md                     # 领域术语表（single-context）
├── .gitignore                     # 含 .env / agent/.env（已忽略，未跟踪）
├── docs/                          # ARCHITECTURE/DESIGN_DETAILS/PROJECT_STRUCTURE/CONFIGURATION/
│                                  #   API_REFERENCE/SECURITY_AND_LIMITATIONS/TESTING/CHANGELOG
│                                  #   + adr/ 架构决策记录 + agents/ agent 指引
├── scripts/                       # repo_cleanup.sh 等运维脚本（本地，不入版本库）
└── agent/                         # 所有源码
    ├── .env / .env.example        # 本地密钥（gitignored）/ 模板
    ├── requirements.txt
    ├── api/
    │   ├── fastapi_server.py      # 唯一入口（1335 行）：SSE + REST + 鉴权
    │   ├── auth.py                # require_auth + TTL 令牌缓存
    │   └── static/                # 静态前端：index.html(登录) app.html(工作台)
    │       ├── js/  app.js auth.js icons.js landing.js
    │       └── css/ app.css auth.css landing.css tokens.css
    ├── agent/                     # 嵌套命名空间包（无 __init__.py，双导入兜底）
    │   ├── react_agent.py         # 智能客服入口
    │   └── tools/  agent_tools.py(15 @tool)  middleware.py(3 中间件)
    ├── agents/                    # 数据分析流水线
    │   ├── base.py  planner_agent.py  sql_agent.py
    │   ├── analysis_agent.py  pipeline_context.py     # 统一分析 Agent + 类型化管道上下文(替 prev_results)
    │   ├── trend_agent.py  product_agent.py  risk_agent.py   # 旧三类(TrendAgent 仍被 quick_data_insight 用;product/risk 已孤儿)
    │   ├── visualization_agent.py  report_agent.py
    │   ├── document_report_agent.py  export_agent.py  query_rewriter.py
    ├── analysis/                  # 纯算法：trend / product / anomaly_detection / analysis_module(Protocol+适配器)
    ├── visualization/ charts.py   # Plotly 图表生成器
    ├── rag/  rag_service.py  vector_store.py  chart_knowledge.py  retrieval_query_rewriter.py
    ├── memory/  short_term.py  long_term.py  summarizer.py  recall.py  service.py  context_budget.py
    ├── model/  factory.py         # 按用户 LLM/Embedding 缓存 + 热重载
    ├── database/                  # duckdb_manager / user_db / user_settings_db
    │   │                          #   datasources_db / data_resolver / schema_loader
    │   └── *.db                   # 6 个 SQLite 运行时文件
    ├── utils/  config_handler  logger_handler  path_tool  prompt_loader
    │           file_handler  request_context  report_exporter  progress_emitter
    ├── config/  rag.yml  chroma.yml  agent.yml  datasources.yml  prompts.yml
    ├── prompts/  main_prompt  report_prompt  document_report  rag_summarize  (.txt)
    ├── templates/  report_template.md   # Jinja2 报告模板
    ├── data/  datasets/  external/  (上传 / 外部数据落盘)
    ├── chroma_db/                 # 向量库持久化
    ├── reports/  charts/          # 生成的报告与图表（挂载到 /reports）
    ├── logs/                      # 按日 .log（无轮转）
    └── tests/                     # 29 个测试文件（216 用例）
```

> 说明：`agent/agent/` 是两层嵌套的命名空间包（`agent/` 与 `agent/agent/` 均无 `__init__.py`），靠命名空间包机制 + 全仓 `try: from agent.x / except: from x` 双导入模式解析，已通过测试验证可运行。
