---
status: implemented
---

# 0005 - Quick-path dataset disambiguation: never guess, ask in text

The `quick_data_insight` tool (single-point data query) used to hand-assemble `SQLAgent` without resolving which dataset the user meant - no `DataResolver.resolve`, no target-CSV preload, empty `primary_table`. With two datasets under one user, a query like "销售额多少" (no dataset-specific words) silently queried whatever the user's DuckDB instance had loaded last and exposed every table's schema to the LLM, producing rows from the wrong dataset when column names collided across tables (user-reported bug).

We decided quick-path data access must go through the same `DataResolver` seam as the pipeline SQL stage, and must **never guess** when the query carries no dataset signal: if resolution hits `dynamic_all` (no keyword matched) with more than one candidate dataset, the tool returns a text list of the user's datasets (display_name + description) asking which one to query, and does not execute SQL. When the query does match, `primary_table` is passed to `SQLAgent` (scoped schema, no cross-table leakage) and the target CSV is preloaded, mirroring `PlannerAgent._resolve_context`.

## Considered

- **A frontend dataset-picker widget** on ambiguous queries was rejected as out of scope: it needs a new SSE message type plus suspend/resume of the in-flight ReAct turn (human-in-the-loop), a cross-frontend/backend protocol change far beyond the C3 boundary. The model can already ask a clarifying question in text; the only cost is one extra turn.
- **Guessing the first dataset** (the pre-fix behaviour) was the bug. It stays rejected on principle: a wrong dataset produces confident wrong numbers - worse than a clarifying question.
- **LLM-generated dataset descriptions** (for better matching) were rejected in favour of rule-generated ones (display_name + row count + column list) written at upload time: no LLM dependency or latency on the upload path, offline-testable. `display_name` (original Chinese filename) already carries most matching signal; descriptions exist primarily so the disambiguation list is readable. Upgradeable later without reshaping the schema.
- **Keeping the static built-in dataset on the old path** was deliberate: `DATASET_MAP` fallback is a single dataset (no cross-table risk), and its `name` is a display name, not a DuckDB table name - passing it as `primary_table` would be wrong.

## Consequences

- `DataResolver.resolve` is unchanged (additive use): its returned candidate list already carried the ambiguity signal (`matched_by == "dynamic_all"` + multiple `datasets`), so no resolver API change was needed - the decision lives in the tool.
- `dataset_service` now writes a rule-generated `description` at upload time (was always `""`); `scripts/backfill_dataset_descriptions.py` (run once, 2026-09-02) backfilled existing rows, skipping cross-user duplicate names because `update_dataset` filters by `name` only while the table constraint is `UNIQUE(owner_user_id, name)`.
- Static built-in datasets keep the previous behaviour (unscoped, single table - no contamination surface).
- Scope guard: "ask in text" is the disambiguation UX; a picker widget remains a product backlog candidate if ambiguous multi-dataset queries prove frequent.
