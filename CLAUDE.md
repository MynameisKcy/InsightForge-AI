# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**InsightForge AI** — a multi-agent collaborative data analysis platform built on LangChain + LangGraph. Users interact via natural language; the system orchestrates specialized agents to perform SQL queries, trend/product/risk analysis, chart generation, and multi-format report export. All source code lives at the repo root (packages: `api/`, `agents/`, `agent/`, `analysis/`, `visualization/`, `rag/`, `memory/`, `model/`, `database/`, `utils/`, ...); the repo root is the sys.path root. The `agent/` package is only the Smart Assistant (ReactAgent + tools).

## Commands

All commands run from the repo root.

**IMPORTANT:** All commands must be run inside the `AnalysisAgent` conda virtual environment. In the bash shell, conda is NOT on PATH by default. Activate it with:
```bash
eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent
```
Every Python/pytest command must be prefixed with this activation sequence.

```bash
# Install dependencies (conda env: AnalysisAgent)
conda activate AnalysisAgent
pip install -r requirements.txt

# Run FastAPI server (recommended, port 8502)
conda activate AnalysisAgent && python -m api.fastapi_server

# Run tests — targeted first (default): only the files related to the change.
# The full suite is ~470 tests / ~56s; run it only when necessary
# (cross-module refactors, or one final green run before claiming completion).
conda activate AnalysisAgent && python -m pytest tests/test_export_agent.py -q   # targeted
conda activate AnalysisAgent && python -m pytest tests/ -q                       # full suite
```

## Git Workflow

- 日常开发一律在 `dev` 分支进行并直接提交——**不再按任务新开分支**。`dev` 上验证无误后合并回 `main`（合并时机与用户确认）。
- 提交信息沿用 conventional commit 前缀 + 中文描述（如 `refactor(export): …`、`fix(api): …`）。
- 推送（`git push`）需用户明确要求后才执行。


## Architecture

### Two-Mode System

- **Smart Assistant mode** (智能客服): `ReactAgent` — LangChain ReAct agent with 15 `@tool` functions and 3 middleware hooks. Conversational, streaming responses. Can invoke the full analysis pipeline via `run_full_analysis` tool.
- **Data Analysis mode** (数据分析): `PlannerAgent` — LLM-generated execution plan (JSON steps with dependencies), then sequential dispatch to specialized agents (Trend/Product/Risk now a unified `AnalysisAgent` + `AnalysisModule` adapters): SQL → Trend/Product/Risk → Visualization → Report → Export.

### Agent Pipeline

All analysis agents extend `BaseAgent` (`agents/base.py`) which provides `_call_llm()` and `_parse_json()`. The `PlannerAgent` orchestrates via `_agent_map` dict, passing results through a typed `PipelineContext` dataclass (`agents/pipeline_context.py`): handlers write typed slots (`pctx.trend_result`, etc.) gated by `completed_steps: set[int]`; the old `prev_results` dict (`step_N` / `agent_name_result` keys) is retired. The Trend/Product/Risk stages are a single `AnalysisAgent` class injected with an `AnalysisModule` adapter (`analysis/analysis_module.py`: `TrendAnalysisAdapter` / `ProductAnalysisAdapter` / `RiskAnalysisAdapter`); `AnalysisAgent.__init__(analyzer, user_id=None, model=None)`. The standalone `TrendAgent`/`ProductAgent`/`RiskAgent` classes are retired (the `quick_data_insight` tool uses `AnalysisAgent(TrendAnalysisAdapter(), user_id=…)`). `SQLAgent` output (`dataframe_json`) is the primary data carrier piped into all downstream agents. The `product_analysis` stage (user-facing label "分组对比分析") is **domain-neutral**: sales data (price+qty columns) uses a revenue=price×qty fast-path, while population/traffic/operations data auto-detects a categorical dimension + numeric measure and aggregates by sum — it no longer forces qty×price on non-sales data. Report tables render data-derived headers (`dimension_col`/`measure_col` metadata) instead of hardcoded "产品/总收入/销量". Internal identifiers (`product_analysis` key, `product_result` slot, `ProductAnalysis` class) are retained as legacy; only labels/behavior were generalized.

### Key Subsystems

- **LLM**: `ChatTongyi` (Qwen/DashScope) + `DashScopeEmbeddings` — module-level singletons in `model/factory.py`. Model names from `.env` (`CHAT_MODEL_NAME`, `EMBEDDING_MODEL_NAME`) with `config/rag.yml` fallback.
- **Database**: DuckDB (`:memory:` per user for OLAP), SQLite (5 files: users.db, customers.db, memory.db, chart_knowledge.db, datasources.db), ChromaDB (vector store in `chroma_db/`).
- **Data Source Management**: Users upload CSV/Excel files via `/api/datasets/upload` or admins pre-configure MySQL/PostgreSQL connections in `config/datasources.yml`. All datasets are loaded into DuckDB for unified querying, including cross-dataset JOINs. Metadata tracked in `datasources.db` (SQLite) via `DatasourcesDB` class. Dataset lifecycle transactions (upload validation → file save → DuckDB load → schema/sample probe → metadata write → failure compensation, plus delete/schema) live in `database/dataset_service.py` (`DatasetService`, constructor-injectable seams); routes in `api/routes/datasets.py` are thin adapters. Sample serialization is unified: `to_json(date_format="iso")` (Timestamp→ISO, NaN→null) on both upload and schema paths.
- **DuckDB Multi-Source**: `duckdb_manager.py` supports `load_csv_dataset()`, `load_excel_dataset()`, `drop_table()`, `get_enhanced_schema_text()` (multi-table schema), and `register_external_databases()` (MySQL/PostgreSQL via DuckDB extensions). `safe_ident()` prevents SQL injection in DuckDB identifiers. Dataset persistence: files on disk, DuckDB tables reloaded on instance creation via `_reload_datasets_into_instance()`.
- **RAG**: Hybrid two-stage retrieval — ChromaDB coarse search (k=15) fused via RRF (k=60) with a BM25 lexical channel (`rag/bm25.py`, jieba + digit-token regex, in-memory index synced by VectorStore write-callbacks, `hybrid_enabled` toggle in `config/rag.yml`) → DashScope `gte-rerank-v2` rerank (top_n=3, threshold=0.3). Deterministic recall metrics (recall@k/MRR) live in `tests/rag_eval/test_recall_metrics.py`. Chart knowledge via SQLite + jieba Chinese tokenization.
- **Memory** (two-tier, [ADR-0003](docs/adr/0003-two-tier-memory-session-and-user-scoped.md)): **Session Memory** - per `session_id` isolated, LRU pool + DB hydration on miss, compression triggered at 90% context budget (not a fixed turn count) with a `summarized_up_to` watermark. **Long-Term Memory** - SQLite (`conversation_history`) + cross-session recall via a ChromaDB `memory` collection (shared-collection + `user_id` owner filter, `gte-rerank-v2` rerank). **`MemoryService`** facade (`memory/service.py`) orchestrates both tiers via `begin_turn()` / `end_turn()`; recall is injected in the `dynamic_prompt` middleware. `ConversationSummarizer` takes an injected `llm_callable` (breaks the memory↔agents cycle).
- **SQL Sandbox**: `_assert_read_only()` whitelist (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN/PRAGMA) + forbidden keyword scan. Management channel (`_load_csv`) bypasses sandbox via `self.conn.execute` directly.
- **Multi-user Isolation**: `contextvars` (`request_context.py`) propagates user_id/session_id through the call chain. Per-user DuckDB instances cached in `_duckdb_instances`. `RequestContext` dataclass on PlannerAgent replaces instance attributes.

### Configuration

| File | Purpose |
|------|---------|
| `.env` | **Primary**: DASHSCOPE_API_KEY, CHAT_MODEL_NAME, EMBEDDING_MODEL_NAME |
| `config/rag.yml` | Rerank model, retrieve_k, rerank_top_n, score threshold |
| `config/chroma.yml` | Collection name, chunk_size=500, chunk_overlap=50, separators |
| `config/agent.yml` | External data path |
| `config/datasources.yml` | Admin-preconfigured database connections (MySQL/PostgreSQL); passwords via `password_env` referencing .env variables |
| `config/prompts.yml` | Paths to prompt template files |
| `prompts/*.txt` | System prompt, RAG prompt, report prompt |
| `data/datasets/` | Uploaded CSV/Excel files stored on disk |
| `data/external/` | External data files referenced by datasources |

**Config truth source**: `.env` is authoritative for model names and API keys; YAML values are fallback only.

### FastAPI SSE Protocol

**Error envelope**: all error responses use `{"success": false, "error": "..."}` via `error_response()` in `api/errors.py`; app-level exception handlers (registered in `fastapi_server`) normalize HTTPException/validation/unhandled exceptions to the same shape. Success bodies are not part of the envelope.

The `/api/chat` endpoint streams events with special tokens: `[THINKING]`, `[SESSION]`, `[SESSIONS_RELOAD]`, `[CHART:url]`, `[STEP:{json}]` (pipeline step progress), `[KEEPALIVE]` (15s heartbeat), `[DONE]`, `[ERROR]`. (`[CONTEXT]` / `[AUDIT:text]` dormant branches removed — backend never emitted them.) Frontend is static files under `api/static/` (served with no-cache middleware + versioned query strings) — no separate frontend build.

### Dataset Management API (new)

- `GET /api/datasets` — list all loaded datasets
- `POST /api/datasets/upload` — upload CSV/Excel (multipart, max 100MB), parses into DuckDB + records metadata
- `DELETE /api/datasets/{name}` — drops DuckDB table + deletes file + removes metadata
- `GET /api/datasets/{name}/schema` — returns columns, statistics (SUMMARIZE), sample rows
- `POST /api/datasources/reload` — hot-reload `datasources.yml` database connections into DuckDB

Frontend sidebar panel (`ds-section`) lists datasets, shows schema on click, and uploads files.

### Report Export API

- `POST /api/report/export` - export report markdown to Word / MD / PDF / HTML (`{markdown, title, format}` -> `FileResponse`). PDF registers Windows Chinese fonts + renders tables. Charts are Plotly interactive HTML in the chat stream but rasterized to PNG (kaleido) at chart-gen time and embedded as real images in all four export formats (Word/PDF embed the PNG file; HTML/MD inline base64 data URIs); the report markdown embeds the PNG web URL so the report bubble also renders the chart. Frontend renders export buttons after a report stream completes; historical session messages re-render markdown / charts / export buttons consistent with the live stream.

## Critical Implementation Details

- **DashScope rerank model**: Must use `gte-rerank-v2` — `gte-rerank` returns 403 for most API keys. Import as `from dashscope import TextReRank` (not `dashscope.Rerank`). Always check `status_code==200` and `output` is not None before accessing results.
- **SQL error retry**: SQLAgent feeds execution errors back to LLM for regeneration (max 2 retries). `_fix_sql` handles this loop.
- **Path resolution**: All file paths are project-relative; `get_abs_path()` in `utils/path_tool.py` resolves relative to the repo root.
- **Config loading**: YAML configs are loaded eagerly at module import time as global dicts (`rag_conf`, `chroma_conf`, etc.) via `utils/config_handler.py`.
- **Reindex**: Use `/api/knowledge/reindex` endpoint or `vs.reindex_all()` to clear and rebuild the vector store.
- **ReactAgent middleware**: `monitor_tool` detects `fill_report_context_for_report` to set `runtime.context["report"]=True`; `report_prompt_switch` (a `@dynamic_prompt` hook) swaps the system prompt to `report_prompt.txt` and, in normal mode (not report mode), injects cross-session recall into the system prompt via `_recall_for_turn`.
- **Multi-table SQL**: `SQLAgent` uses `get_enhanced_schema_text()` to inject ALL loaded tables into the prompt (not a single hardcoded table); `get_enhanced_schema_text` now emits per-column semantic stats + wide-table flags (via `_compute_table_profile`, instance-cached, cache cleared on table rebuild). `DataResolver` resolves datasets dynamically from `datasources_db` by `display_name` (Chinese original names preserved; "山东" matches "山东省...") with fallback to the hardcoded `DATASET_MAP`. `get_data_overview` tool iterates all tables in DuckDB.
- **DuckDB identifiers**: Always use `safe_ident()` when interpolating table names into DuckDB SQL. Direct f-string interpolation of user-controlled identifiers is a SQL injection risk.
- **CSV encoding fallback**: `load_csv_dataset` and the management-channel `_load_csv` fall back to pandas multi-encoding (GBK/GB18030/UTF-8) decode when DuckDB `read_csv_auto` fails on non-UTF-8 Chinese CSVs.
- **Chart PNG export (kaleido)**: Charts are interactive Plotly HTML in the chat; a sibling PNG is also generated (kaleido) for report export. Do **not** use Plotly's `fig.write_image()` -- it opens a fresh kaleido scope per call and the 2nd call in the same process hangs (watchdog-killed). Use the persistent sync server in `visualization/charts.py`: `start_png_batch()` → `kaleido.write_fig_sync(fig, path, opts_dict)` (plain-dict opts). The server stays resident for the process lifetime -- `stop_png_batch()` is a deliberate no-op because `kaleido.stop_sync_server()` triggers a fatal GIL error at interpreter exit; the OS reaps chromium on process end.

## Agent skills

### Issue tracker

Issues live in GitHub Issues (repo `MynameisKcy/Multi-Agent-Data-Analysis-System`), operated via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles, label string equal to role name (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
