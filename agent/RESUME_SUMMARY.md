# InsightForge AI — 简历项目总结

## 项目名称

**InsightForge AI** — 基于多智能体协作的 AI 数据分析平台

## 一句话描述

基于 LangChain/LangGraph 的多 Agent 协作数据分析系统，支持自然语言交互、自动化趋势/产品/风险分析、Plotly 交互式图表及多格式报告导出。

## 技术栈

**编程语言**: Python 3.10

**AI & LLM**: LangChain, LangGraph, 通义千问 (Qwen3-Max), DashScope Embeddings, ChromaDB (向量检索)

**数据处理**: DuckDB (OLAP), Pandas, NumPy

**数据可视化**: Plotly (折线图/柱状图/饼图/热力图/散点图)

**Web 框架**: FastAPI (SSE 流式响应), Streamlit, Jinja2 模板引擎

**报告导出**: python-docx (Word), ReportLab (PDF)

**数据库**: SQLite (用户系统 + 图表知识库 + 会话持久化)

**工程化**: 多模块分层架构, YAML 配置管理, Logger 日志系统, 工厂模式

## 核心功能

1. **多 Agent 协同架构** — 7 个专业 Agent 分工协作（任务规划 / SQL查询 / 趋势分析 / 产品分析 / 风险分析 / 图表生成 / 报告导出），通过 Planner Agent 统一编排调度
2. **自然语言数据分析** — 用户以自然语言描述需求，系统自动理解意图、生成 SQL、执行分析、输出结论，实现零代码数据探索
3. **RAG 知识库问答** — 基于 ChromaDB 向量检索 + LLM 总结，支持 txt/pdf 知识库嵌入，实现文档级精准问答
4. **交互式图表自动生成** — 根据数据特征自动选择图表类型，Plotly 生成可交互的 HTML 图表，支持缩放、悬停、筛选
5. **完整报告自动生成** — 整合所有分析模块结果，Jinja2 模板渲染结构化 Markdown 报告，一键导出 Word/PDF/HTML
6. **流式对话 + 记忆系统** — SSE 流式响应配合打字机效果，短期记忆维护会话上下文，长期记忆持久化对话历史
7. **用户认证与会话管理** — Token 认证体系，多会话切换，对话历史可追溯

## 个人贡献（示例要点）

- 设计并实现了 7 个 Agent 的多智能体协作架构，完成从任务规划到报告导出的全链路自动化
- 基于 LangGraph 构建 ReactAgent 入口层，集成 10+ 个功能工具，实现意图识别与自动调度
- 实现 SSE 流式输出，支持思考状态实时反馈和图表内嵌展示，优化用户体验
- 构建短期/长期双层记忆系统，支持对话上下文保持和历史会话持久化
- 实现 Plotly 图表自动选型引擎，支持 5 种图表类型并按数据特征智能匹配
- 设计 ChromaDB + LLM 的 RAG 检索增强管线，实现知识库精准问答
- 搭建 FastAPI Web 服务 + Streamlit 双前端，实现前后端分离架构

---

## 技术架构图

```
User → FastAPI/Streamlit → ReactAgent (LangGraph)
                                │
            ┌───────────────────┼───────────────────┐
            ▼                   ▼                   ▼
      RAG 知识库          数据分析工具        外部数据查询
      (ChromaDB)      (run_full_analysis)    (CSV records)
                                │
                    Planner Agent (编排调度)
                      /    |    |    \    \
                     ▼     ▼    ▼     ▼    ▼
                   SQL  Trend Product Risk Viz Report Export
                  Agent Agent Agent  Agent Agent Agent Agent
```

## 关键数据

- **Agent 数量**: 8 个（含 Planner 编排）
- **工具数量**: 11 个（RAG、数据分析、图表知识库、用户报告等）
- **图表类型**: 5 种（折线图、柱状图、饼图、热力图、散点图）
- **导出格式**: 4 种（Markdown、Word、PDF、HTML）
- **代码规模**: ~5000+ 行 Python，20+ 模块
