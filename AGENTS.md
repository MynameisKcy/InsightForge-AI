# AGENTS.md

Guidance for ZCode agents working in this repo. Deep implementation details live in
`CLAUDE.md` (same level of authority as this file) — read it before touching
`visualization/`, `memory/`, the SQL sandbox, or the report-export path.

## Project

**InsightForge AI** — multi-agent collaborative data-analysis platform (LangChain 1.3 +
LangGraph 1.2, FastAPI SSE, DashScope/Qwen). Natural-language questions are routed by a
Smart Assistant (ReactAgent) that either answers directly or invokes the Analysis
Pipeline: PlannerAgent → SQL → Trend/Product/Risk (one `AnalysisAgent` +
`AnalysisModule` adapters) → Visualization → Report → Export. All Python packages live
at the repo root (flat layout): `api/`, `agents/`, `agent/`, `analysis/`,
`visualization/`, `rag/`, `memory/`, `model/`, `database/`, `utils/`, `config/`,
`prompts/`, `tests/`.

- `agent/` is ONLY the Smart Assistant package (ReactAgent + its 15 tools). The
  specialized pipeline agents live in `agents/`. Do not confuse the two.
- `agents/base.py` (`BaseAgent`) provides `_call_llm()` / `_parse_json()`; all pipeline
  agents extend it. The planner passes state through the typed `PipelineContext`
  dataclass (`agents/pipeline_context.py`) — the old `prev_results` dict is retired.
- `SQLAgent` output (`dataframe_json`) is the data carrier piped into all downstream agents.

## Commands

Run everything from the repo root, inside the `AnalysisAgent` conda env. In Git Bash,
conda is NOT on PATH — prefix every command with:

```bash
eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent
```

(System Python is 3.8 — too old; the project needs the env's Python 3.10+.)

```bash
python -m api.fastapi_server        # FastAPI server, port 8502, serves api/static/ frontend
python -m pytest tests/ -v          # full suite (~60s, offline, LLM 100% mocked)
python -m pytest tests/test_rag_service.py -v   # single file
```

- Use **pytest**, not `python -m unittest discover` (misses pytest-style function tests).
- No lint/typecheck tooling is configured (no ruff/mypy/black configs).

## Import & path rules

- Repo root IS the sys.path root (`conftest.py` bootstraps it for pytest). Use bare
  imports: `from agents.base import BaseAgent`, `from utils.path_tool import get_abs_path`
  — never `src.`-style prefixes or sys.path hacks inside modules.
- Resolve file paths via `get_abs_path()` in `utils/path_tool.py` (project-relative), never cwd.
- YAML configs load eagerly at import time as module-level dicts (`utils/config_handler.py`).

## Critical gotchas

- **kaleido/Plotly PNG**: never call `fig.write_image()` (2nd call hangs the process).
  Use the persistent sync server in `visualization/charts.py`:
  `start_png_batch()` → `kaleido.write_fig_sync(fig, path, opts_dict)`.
  `stop_png_batch()` is a deliberate no-op (stopping triggers a fatal GIL error at exit).
- **DashScope rerank**: model must be `gte-rerank-v2` (`gte-rerank` returns 403); import
  `from dashscope import TextReRank`; check `status_code==200` and `output` before use.
- **DuckDB identifiers**: always wrap interpolated table names in `safe_ident()`
  (`database/safety.py`) — direct f-strings are a SQL injection risk. The read-only
  AST sandbox (`_assert_read_only`) guards the query channel; the `_load_csv` /
  `load_csv_dataset` management channel legitimately bypasses it. Query-channel
  resource limits (memory/threads/rows/timeout) live in `config/agent.yml` `duckdb:`
  and are applied via connect-time config + Python-side caps — SET/PRAGMA stay
  sandbox-rejected, so never try to apply limits via SQL.
- **CSV encoding**: Chinese CSVs may fail `read_csv_auto`; the pandas GBK/GB18030/UTF-8
  fallback in `load_csv_dataset` handles this — keep it.
- **Config priority**: `.env` is authoritative for model names/API keys (web-app settings
  override it at runtime; YAML values are fallback only).
- **Multi-user isolation**: user_id/session_id propagate via `contextvars`
  (`utils/request_context.py`); per-user DuckDB `:memory:` instances. Never cache
  user-scoped state in module globals.
- **SSE protocol**: `/api/chat` streams special tokens (`[STEP:{json}]`, `[CHART:url]`,
  `[DONE]`, …). Changing the stream format requires updating `api/static/` JS in lockstep.
- **Windows-first**: PDF export registers Windows Chinese fonts; kaleido/chromium paths
  assume Windows. Verify cross-platform claims before relying on them.

## Conventions

- Commits: conventional-commit prefix + Chinese subject, e.g.
  `refactor(database): 导入统一 bare + 折叠 2 shim (候选4-P1)`. Work happens on `dev`;
  `main` is the PR target.
- Tests are offline with LLM/external services 100% mocked; DB tests use temp
  SQLite / in-memory DuckDB. New tests must keep the suite offline-runnable.
- UI copy and user-facing labels are Simplified Chinese; code identifiers are English.
- The `product_analysis` stage (label "分组对比分析") is domain-neutral: it auto-detects
  sales (price×qty) vs. categorical-dimension data. Internal names
  (`product_analysis`, `product_result`, `ProductAnalysis`) are legacy — don't take
  them as evidence of sales-only behavior.

## Read before editing sensitive areas

- `CONTEXT.md` — domain glossary (canonical terms: Smart Assistant, Analysis Pipeline,
  Session Memory, Long-Term Memory, SQL Safety; avoid the listed anti-terms).
- `docs/ARCHITECTURE.md`, `docs/DESIGN_DETAILS.md` — system design.
- `docs/adr/` — e.g. ADR-0003 (two-tier memory) before touching `memory/`.
- `docs/SECURITY_AND_LIMITATIONS.md` — security model and known limits.
- `docs/agents/issue-tracker.md` — issues live in GitHub Issues
  (`MynameisKcy/Multi-Agent-Data-Analysis-System`) via `gh`; triage labels in
  `docs/agents/triage-labels.md`.
