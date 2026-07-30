---
status: accepted
---

# 0002 - Query rewriting at two points: coreference at the analysis bridge, multi-query at RAG retrieval

The system passes the raw user query straight into both the analysis pipeline (via `run_full_analysis`) and RAG retrieval (`rag_service.retriever_docs`), with no transformation. We add query rewriting as two focused standalone components, because the two points need different transformations:

- **Analysis bridge** (`QueryRewriter`, called at the top of `PlannerAgent.run` before `_create_plan`): resolves conversational coreference against recent chat history (`get_session(user_id).get_context`) into one self-contained query. Fixes multi-turn queries like "分析它的趋势" reaching the planner with "它" unresolved.
- **RAG retrieval** (`RetrievalQueryRewriter`, called inside `retriever_docs` before coarse recall): multi-query expansion - generates N=3 paraphrases, retrieves coarse candidates for each, unions + dedups, then feeds the union to the existing rerank (which still scores against the original query). Widens recall; rerank keeps precision.

## Considered

- **Bridge**: instructing the ReactAgent to pass a self-contained query inline (free, but non-deterministic and untestable) was rejected for a dedicated step. Coreference is a correctness feature and deserves determinism + a unit test; the cost is one extra LLM call per analysis, negligible against a multi-minute pipeline.
- **Retrieval**: HyDE (embed a hypothetical answer) was rejected for multi-query, which composes with the existing coarse-recall -> rerank flow without changing what gets embedded, and is robust to bad paraphrases (rerank filters them) where a bad HyDE answer actively misleads. Keyword expansion was rejected as too weak.
- Rewriting for recall is **not** redundant with rerank: rerank orders and selects from what coarse recall returned, so it cannot recover a relevant doc that recall never fetched. Rewriting widens the pool rerank sees.

## Consequences

- Both rewriters fall back to the original query on LLM failure (mirrors rerank's degradation) - no new hard failure mode.
- Short-term memory is keyed by `user_id`, not session, so the bridge rewriter's history can leak across sessions. Accepted as a known limitation; candidate for a separate per-session-memory feature.
- Extra LLM call per analysis (bridge) and per retrieval (expansion). Accepted: this is a personal project optimizing for depth over latency.
