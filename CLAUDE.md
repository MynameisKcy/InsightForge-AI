# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**InsightForge AI** — a multi-agent collaborative data analysis platform built on LangChain + LangGraph. Users interact via natural language; the system orchestrates specialized agents to perform SQL queries, trend/product/risk analysis, chart generation, and multi-format report export. All source code lives under `agent/`.

## Commands

**IMPORTANT:** All commands must be run inside the `AnalysisAgent` conda virtual environment. In the bash shell, conda is NOT on PATH by default. Activate it with:
```bash
eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent
```
Every Python/pytest command must be prefixed with this activation sequence.

```bash
# Install dependencies (conda env: AnalysisAgent)
conda activate AnalysisAgent
cd agent
pip install -r requirements.txt

# Run FastAPI server (recommended, port 8502)
conda activate AnalysisAgent && cd agent && python -m api.fastapi_server

# Run Streamlit UI (port 8501)
conda activate AnalysisAgent && cd agent && streamlit run app.py

# Run existing tests
conda activate AnalysisAgent && cd agent && python -m pytest tests/ -v
# or: python -m unittest discover tests -v

# Run a single test file
conda activate AnalysisAgent && cd agent && python -m unittest tests/test_rag_service.py -v
```

## Architecture

### Two-Mode System

- **Smart Assistant mode** (智能客服): `ReactAgent` — LangChain ReAct agent with 13 `@tool` functions and 3 middleware hooks. Conversational, streaming responses. Can invoke the full analysis pipeline via `run_full_analysis` tool.
- **Data Analysis mode** (数据分析): `PlannerAgent` — LLM-generated execution plan (JSON steps with dependencies), then sequential dispatch to specialized agents: SQL → Trend/Product/Risk → Visualization → Report → Export.

### Agent Pipeline

All analysis agents extend `BaseAgent` (`agent/agents/base.py`) which provides `_call_llm()` and `_parse_json()`. The `PlannerAgent` orchestrates via `_agent_map` dict, passing results through `prev_results` (keyed by `step_N` and `agent_name_result`). `SQLAgent` output (`dataframe_json`) is the primary data carrier piped into all downstream agents.

### Key Subsystems

- **LLM**: `ChatTongyi` (Qwen/DashScope) + `DashScopeEmbeddings` — module-level singletons in `model/factory.py`. Model names from `.env` (`CHAT_MODEL_NAME`, `EMBEDDING_MODEL_NAME`) with `config/rag.yml` fallback.
- **Database**: DuckDB (`:memory:` per user for OLAP), SQLite (5 files: users.db, customers.db, memory.db, chart_knowledge.db, datasources.db), ChromaDB (vector store in `chroma_db/`).
- **Data Source Management**: Users upload CSV/Excel files via `/api/datasets/upload` or admins pre-configure MySQL/PostgreSQL connections in `config/datasources.yml`. All datasets are loaded into DuckDB for unified querying, including cross-dataset JOINs. Metadata tracked in `datasources.db` (SQLite) via `DatasourcesDB` class.
- **DuckDB Multi-Source**: `duckdb_manager.py` supports `load_csv_dataset()`, `load_excel_dataset()`, `drop_table()`, `get_enhanced_schema_text()` (multi-table schema), and `register_external_databases()` (MySQL/PostgreSQL via DuckDB extensions). `safe_ident()` prevents SQL injection in DuckDB identifiers. Dataset persistence: files on disk, DuckDB tables reloaded on instance creation via `_reload_datasets_into_instance()`.
- **RAG**: Two-stage retrieval — ChromaDB coarse search (k=15) → DashScope `gte-rerank-v2` rerank (top_n=3, threshold=0.3). Chart knowledge via SQLite + jieba Chinese tokenization.
- **Memory**: Short-term (in-memory, 30-turn window with LLM compression) + Long-term (SQLite).
- **SQL Sandbox**: `_assert_read_only()` whitelist (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN/PRAGMA) + forbidden keyword scan. Management channel (`_load_csv`) bypasses sandbox via `self.conn.execute` directly.
- **Multi-user Isolation**: `contextvars` (`request_context.py`) propagates user_id/session_id through the call chain. Per-user DuckDB instances cached in `_duckdb_instances`. `RequestContext` dataclass on PlannerAgent replaces instance attributes.

### Configuration

| File | Purpose |
|------|---------|
| `agent/.env` | **Primary**: DASHSCOPE_API_KEY, CHAT_MODEL_NAME, EMBEDDING_MODEL_NAME |
| `agent/config/rag.yml` | Rerank model, retrieve_k, rerank_top_n, score threshold |
| `agent/config/chroma.yml` | Collection name, chunk_size=500, chunk_overlap=50, separators |
| `agent/config/agent.yml` | External data path |
| `agent/config/datasources.yml` | Admin-preconfigured database connections (MySQL/PostgreSQL); passwords via `password_env` referencing .env variables |
| `agent/config/prompts.yml` | Paths to prompt template files |
| `agent/prompts/*.txt` | System prompt, RAG prompt, report prompt |
| `agent/data/datasets/` | Uploaded CSV/Excel files stored on disk |
| `agent/data/external/` | External data files referenced by datasources |

**Config truth source**: `.env` is authoritative for model names and API keys; YAML values are fallback only.

### FastAPI SSE Protocol

The `/api/chat` endpoint streams events with special tokens: `[THINKING]`, `[SESSION]`, `[SESSIONS_RELOAD]`, `[CHART:url]`, `[CONTEXT]`, `[AUDIT:text]`, `[DONE]`, `[ERROR]`. Frontend is embedded HTML in `fastapi_server.py` — no separate frontend build.

### Dataset Management API (new)

- `GET /api/datasets` — list all loaded datasets
- `POST /api/datasets/upload` — upload CSV/Excel (multipart, max 100MB), parses into DuckDB + records metadata
- `DELETE /api/datasets/{name}` — drops DuckDB table + deletes file + removes metadata
- `GET /api/datasets/{name}/schema` — returns columns, statistics (SUMMARIZE), sample rows
- `POST /api/datasources/reload` — hot-reload `datasources.yml` database connections into DuckDB

Frontend sidebar panel (`ds-section`) lists datasets, shows schema on click, and uploads files.

## Critical Implementation Details

- **DashScope rerank model**: Must use `gte-rerank-v2` — `gte-rerank` returns 403 for most API keys. Import as `from dashscope import TextReRank` (not `dashscope.Rerank`). Always check `status_code==200` and `output` is not None before accessing results.
- **SQL error retry**: SQLAgent feeds execution errors back to LLM for regeneration (max 2 retries). `_fix_sql` handles this loop.
- **Path resolution**: All file paths are project-relative; `get_abs_path()` in `utils/path_tool.py` resolves relative to `agent/` directory.
- **Config loading**: YAML configs are loaded eagerly at module import time as global dicts (`rag_conf`, `chroma_conf`, etc.) via `utils/config_handler.py`.
- **Reindex**: Use `/api/knowledge/reindex` endpoint or `vs.reindex_all()` to clear and rebuild the vector store.
- **ReactAgent middleware**: `monitor_tool` detects `fill_report_context_for_report` to set `runtime.context["report"]=True`; `report_prompt_switch` then swaps the system prompt to `report_prompt.txt`.
- **Multi-table SQL**: `SQLAgent` uses `get_enhanced_schema_text()` to inject ALL loaded tables into the prompt (not a single hardcoded table). `DataResolver` resolves datasets dynamically from `datasources_db` with fallback to the hardcoded `DATASET_MAP`. `get_data_overview` tool iterates all tables in DuckDB.
- **DuckDB identifiers**: Always use `safe_ident()` when interpolating table names into DuckDB SQL. Direct f-string interpolation of user-controlled identifiers is a SQL injection risk.
