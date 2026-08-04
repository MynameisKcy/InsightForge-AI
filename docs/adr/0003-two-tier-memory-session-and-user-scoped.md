---
status: accepted
---

# 0003 - Two-tier memory: session-scoped Session Memory + user-scoped Long-Term Memory

The memory subsystem had one keying scheme for working memory (the in-memory `_session_pool`, keyed by `user_id`) and another for the persisted turn log (`conversation_history`, keyed by `session_id`). The mismatch let one user's sessions share working context - the cross-session leak ADR-0002 flagged - and working memory was never rehydrated from the persisted log after a restart or session switch, so the LLM lost session context while SQLite held the full history.

We decided to make the mismatch a deliberate two-tier boundary rather than eliminate it. **Session Memory** is session-scoped: the pool is keyed by `session_id`, rebuilt from `conversation_history` on miss, and compacted by a context-usage trigger (90% of model context) measured with the model's own `usage_metadata` plus a reactive overflow-retry; a persisted per-session watermark (`summarized_up_to`) means the LLM sees "latest summary + post-watermark turns" while the full history stays for display. **Long-Term Memory** is user-scoped: each session's final summary is embedded into a shared ChromaDB collection - reusing the existing `VectorStoreService` `user_id` owner-filter pattern, not a per-user collection - and retrieved (via the existing RAG two-stage retrieval) by relevance to the current query, so a session can recall conclusions from the user's other sessions. `user_id` is the ownership/isolation key throughout; `session_id` is the working-memory key.

## Considered

- **Eliminate user-scoping entirely** (all memory session-scoped) was rejected: it fixes the leak but leaves no user-scoped store to recall from, killing cross-session recall.
- **Keep the single user-scoped pool, add hydration only** was rejected: it leaves the cross-session leak intact.
- **Always-inject / raw-turn corpus** for Long-Term Memory was rejected for retrieval-over-summaries: injecting all recent summaries doesn't scale and injects irrelevant context; embedding raw turns is a noisy full-text search rather than recall of conclusions. Summaries (one per session, written on compaction and on session end) keep the corpus bounded and recall conclusions.
- **Per-user ChromaDB collections** (`memory_{user_id}`) for the recall store were rejected for a shared collection with `user_id` owner filtering: the existing RAG already isolates equally-private per-user knowledge that way (with regression tests in `test_vector_store_isolation.py`), and a per-user model would introduce a second isolation model and rework the existing owner/md5/reindex machinery for no meaningful safety gain when retrieval is funneled through the service.

## Consequences

- `memory_summaries` gains a `session_id` (session-scoped writes); it is read session-scoped for Session Memory hydration and user-scoped for Long-Term retrieval - one table, two read patterns.
- Session Memory's fixed `max_turns` cap is replaced by the 90% context budget for the chat path; the QueryRewriter bridge keeps a small fixed window (coreference needs only recent turns).
- Each chat turn pays one embedding + one rerank for Long-Term retrieval (no extra LLM call); accepted, consistent with ADR-0002's "depth over latency" stance.
- The recall store reuses the shared-collection + `user_id` owner-filter pattern (a separate `memory` collection via the existing `VectorStoreService`); recall retrieval filters with `include_public=False` (strictly per-user, no public memory). Remove a session's embedding (by `session_id` metadata) when the session is deleted.
- `session_id` must be resolved (created if absent) before any memory access, and threaded into the ReactAgent; the in-memory pool is LRU-bounded and evicted on session deletion.
- Resolves the "short-term memory keyed by `user_id`, leaks across sessions" limitation recorded in ADR-0002.
