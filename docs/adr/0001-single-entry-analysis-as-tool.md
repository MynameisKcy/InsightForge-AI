---
status: accepted
---

# 0001 - Single entry point: analysis as a tool, not a peer mode

The product presents as two modes — a conversational Smart Assistant (ReactAgent) and a Data Analysis pipeline (PlannerAgent) — but in the running system only ReactAgent is wired to the frontend. The pipeline runs when ReactAgent's ReAct loop calls the `run_full_analysis` tool, and the direct `/api/analysis` endpoint is never called by the frontend.

We decided to commit to that single-entry reality: ReactAgent is the only entry point, `run_full_analysis` is the canonical way to run analysis, and `/api/analysis` is retired. The planning + sub-agent orchestration capability the pipeline was built for is preserved — it is just invoked as a tool rather than entered as a peer mode.

## Considered

A genuine two-mode split — an intent router at `/api/chat` dispatching to ReactAgent or PlannerAgent as peers, each with its own streaming contract — was rejected. It would classify the same natural-language query twice (the router vs. the ReAct loop's own tool choice), giving two places to be wrong about intent for no user-visible gain, while adding a second entry, streaming, and session-handling path to maintain.

## Consequences

- The analysis pipeline runs synchronously inside the ReactAgent turn. A cross-thread `ProgressEmitter` keeps the SSE stream alive with `[KEEPALIVE]`/`[STEP]` events during the blocking call; this is an accepted workaround, not a defect to remove.
- The `run_full_analysis` tool constructs the PlannerAgent per-user: `agent_tools._get_or_create_analyst(user_id)` caches a `PlannerAgent(user_id)` per user (built with the user's model config, not the default), and `run_full_analysis` feeds it the current request's `user_id` from the request contextvar. The retired `/api/analysis` route and the tool share this one cache; `invalidate_analyst(user_id)` (called from `_invalidate_user_agents` on config save) drops a user's instance so the next run rebuilds it on the new config.
- Analysis cannot have a streaming/UX shape different from chat — an accepted cost of single-entry.
