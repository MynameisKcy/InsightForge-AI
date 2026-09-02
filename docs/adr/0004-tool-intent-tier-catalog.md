---
status: implemented
---

# 0004 - Tool intent tiers: one catalog table derives every toolset variant

The Smart Assistant binds twelve tools into its ReAct agent (`react_agent.py`), while `middleware.py` re-imports the same twelve tools and hand-maintains three nested toolset constants (`CHAT_TOOLS ⊂ QUERY_TOOLS ⊂ ANALYSIS_TOOLS`) to trim the model-visible tools per intent (`dynamic_toolset`). Adding one tool means editing two lists (the agent's binding list and the middleware tiers); forgetting a list silently removes the tool from one intent or leaks a heavy tool into chat. The tier lists have zero direct test coverage - only the middleware that reads them.

We decided that an intent tier is a property of the tool itself - its **minimum visible tier** (`min_intent`) on a monotone ladder `chat < query < analysis` - recorded in a single catalog table in `agent_tools.py` (same module as the `@tool` definitions), with one derivation entry `for_intent(intent)` as the only consumer interface. `ANALYSIS` tier = the full catalog = the ToolNode-bound set; `react_agent` binds `for_intent(Intent.ANALYSIS)` and `dynamic_toolset` calls `for_intent(intent)` per request. Tiers become a structural rank rule (`rank(min_intent) <= rank(intent)`), not hand-written nested lists, so the ladder cannot silently drift.

## Considered

- **A separate `agent/tools/registry.py` module** was rejected after verification: it re-imports `agent_tools` for the tool objects anyway, so the heavy import chain (planner and friends) is unchanged - an extra file with zero decoupling gain. The catalog lives at the bottom of `agent_tools.py` for locality with the tool definitions.
- **Attaching `min_intent` as an attribute on each `@tool` object** was rejected as infeasible: a `@tool` object is a pydantic `StructuredTool`, and assigning an undeclared field raises `ValueError: object has no field "min_intent"` (verified on langchain-core 1.4.9). A parallel catalog table is the only workable shape.
- **Three hand-written tier lists** (one per intent) carry the same information as the dict, but let one tool accidentally appear in two lists - reintroducing the duplication this ADR removes.
- **A separate `all()` entry alongside `for_intent`** was rejected for a single entry: `analysis` is by construction the full catalog, so two entries would invite the "bound but invisible to every intent" ambiguity. Registered tool = Smart Assistant tool = ToolNode-bound set is one identity.
- **Moving the `Intent` enum out of `intent_router.py`** was rejected: the classifier module stays lightweight (top-level import of `utils.logger` only), and the tool catalog imports the enum from it rather than dragging the tool dependency graph into intent classification.

## Consequences

- One place to declare a tool's tier; adding a tool = definition + one catalog line. Drift between binding list and tiers is structurally impossible.
- The ladder invariant (`chat ⊂ query ⊂ analysis`, `analysis = full set`) is derived from the rank rule and pinned by contract assertions folded into existing tests - no new test file, net-neutral or net-negative test count.
- `middleware.py` loses its duplicate 12-tool import block and the `CHAT_TOOLS` / `QUERY_TOOLS` / `ANALYSIS_TOOLS` / `_TOOLSETS` / `_select_toolset` constants; `react_agent.py` loses its 12-name binding list.
- `intent_router.py`'s docstring (lines 11-14) stops enumerating deleted constants and describes the intent tiers' meaning instead.
- The ToolNode-binds-full-set + per-request `override(tools=subset)` division of labour is unchanged.
- Scope guard: `status_text` / `local_tool_names` (react display hints) stay owned by the display concern and are not folded in. `mode_effect` (report-mode switch) was deliberately NOT folded into the initial C1 scope; it landed later in the same session as the sparse Extension table above - the catalog shape absorbed it without reshaping.

## Implementation

- Catalog landed as an ordered `(tool, min_intent)` list (`_TOOL_MIN_INTENT`) plus `_INTENT_RANK` and `for_intent()` at the bottom of `agent_tools.py`. A `dict` keyed by tool object was attempted first but failed verification: a `@tool` object is a pydantic `StructuredTool`, which is unhashable (`TypeError: unhashable type`), so a list of pairs is used instead.
- Consumers updated: `react_agent.py` binds `for_intent(Intent.ANALYSIS)`; `middleware.py` drops the duplicate 12-tool import and the `CHAT_TOOLS` / `QUERY_TOOLS` / `ANALYSIS_TOOLS` / `_TOOLSETS` / `_select_toolset` constants and calls `for_intent(intent)` directly; `intent_router.py` docstring no longer enumerates the deleted constants.
- Contract assertions (`TestToolCatalogContract`, four cases: ladder nesting / analysis = full catalog / key membership / identity stability) folded into the existing `tests/test_intent_router.py`; no new test file.
- Verified: `for_intent` yields 3 / 10 / 12 tools for chat / query / analysis (identical membership to the retired constants); full suite 651 passed (was 647 - the four new cases) with no regressions.

## Extension (same session): tool mode effects

The catalog gained a second, sparse declaration table for **mode effects** - the tools that, when invoked, must flip a runtime-context flag. The report-mode trigger previously worked by a magic string in `monitor_tool` (`if tool_name == 'fill_report_context_for_report': context["report"] = True`), which silently broke on a tool rename and had no write-side test.

- `agent_tools.py` declares `REPORT_MODE = "report"` and `_TOOL_MODE_EFFECT = [(fill_report_context_for_report, REPORT_MODE)]` beside the intent catalog (object-aligned, no string-typo surface), and exports `mode_effect_for(tool_name)`.
- `monitor_tool` replaces the name match with `effect = mode_effect_for(tool_name); if effect: runtime.context[effect] = True` - the effect *is* the context key, so the transition is generic and a rename touches only the catalog line.
- The context key is now owned once: the `"report"` literal was read/written/seeded in three places (`monitor_tool` write, `_build_system_prompt` read, `react_agent` seed); all three now reference `REPORT_MODE`.
- A sparse table (only tools with an effect appear) rather than widening the intent catalog: `min_intent` is an attribute of every tool, `mode_effect` of very few - the shape matches the data.
- Tests folded into existing files: `ToolInvokeMiddlewareTests` gained write-side cases (effect tool sets the flag; a normal tool leaves context untouched), `TestToolCatalogContract` pins the sparse-table contract.
