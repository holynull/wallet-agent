# Production ReAct Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate production ReAct conversations from deterministic wallet lifecycle execution and add bounded observability, evaluation, and safe read caching.

**Architecture:** Build two LangGraph compilations over the same serializable `AgentState` and checkpointer, route them through a `GraphRouter` facade, and keep the existing API transport unchanged. Add strict production capability checks, per-tool timing/budget metadata, offline evaluation cases, and a narrow metadata cache.

**Tech Stack:** Python 3.11, LangGraph 0.6.11, LangChain Core 0.3.86, FastAPI, pytest, Ruff, Next.js demo.

**Spec:** `docs/superpowers/specs/2026-10-08-production-react-hardening-design.md`

## Global Constraints

- Ordinary natural-language routing must never silently fall back to legacy in production.
- ReAct tools may query or prepare but never sign, broadcast, or bypass explicit confirmation.
- Both graphs share the same `thread_id` and checkpointer; state remains JSON/checkpoint safe.
- Tests must use deterministic fake models/providers and must cover termination and resume boundaries.
- Every production behavior change is introduced by a failing regression test first.

---

### Task 1: Split compiled conversation and lifecycle graphs

**Files:**
- Modify: `src/wallet_agent/graph/build.py`
- Modify: `src/wallet_agent/graph/state.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/graph/__init__.py`
- Modify: `src/wallet_agent/main.py`
- Test: `tests/graph/test_graph_modes.py`
- Test: `tests/persistence/test_recovery.py`

**Interfaces:**
- `build_graph(..., split_graphs: bool = False, require_react: bool = False) -> CompiledGraph | GraphRouter`
- `GraphRouter.ainvoke/astream/aget_state/get_state/aupdate_state` preserve the compiled-graph methods used by the API.
- `GraphRouter.conversation_graph` and `.lifecycle_graph` expose their roles for diagnostics.

- [x] Write a failing test proving a strict production graph rejects a model without `invoke_react` before serving ordinary chat.
- [x] Write a failing test proving a split graph sends natural language to the conversation graph and forced/resume inputs to the lifecycle graph.
- [x] Write a failing test proving a lifecycle interrupt created by one compiled graph resumes through the router after rebuilding with the same checkpointer.
- [x] Implement the router and two graph builders while preserving the current `build_graph` compatibility default for existing unit doubles.
- [x] Update production `build_application` to enable `split_graphs=True` and `require_react=True`.
- [x] Run the focused graph and persistence tests.

### Task 2: Enforce route capability and add route telemetry

**Files:**
- Modify: `src/wallet_agent/config.py`
- Modify: `src/wallet_agent/graph/build.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/api/app.py`
- Test: `tests/test_config.py`
- Test: `tests/graph/test_graph_modes.py`
- Test: `tests/api/test_api_contract.py`

**Interfaces:**
- `Settings.react_compatibility_mode: bool = False` controls only explicit migration/test fallback.
- State fields `graph_role`, `route_mode`, and `route_reason` are JSON-safe and appear in streamed final state.

- [x] Add a failing configuration test for the strict default and explicit compatibility opt-in.
- [x] Add a failing API contract test asserting route metadata is serialized without LangChain objects.
- [x] Implement startup capability validation and explicit compatibility behavior.
- [x] Emit route metadata on both ordinary and lifecycle runs.
- [x] Run focused configuration/API tests.

### Task 3: Instrument and bound ReAct tool execution

**Files:**
- Modify: `src/wallet_agent/graph/state.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/models/registry.py`
- Modify: `src/wallet_agent/api/app.py`
- Test: `tests/graph/test_unified_react.py`
- Test: `tests/api/test_sse_recovery.py`

**Interfaces:**
- State field `react_tool_timings: list[dict[str, Any]]` contains only tool name, call id, elapsed milliseconds, status, and safe error code.
- `GraphRuntime.max_react_steps` and `provider_timeout_seconds` bound model/tool work.

- [x] Write failing tests for a timed-out ReAct tool, a successful timing record, and a step-budget terminal response.
- [x] Wrap ToolNode execution with a bounded timing/error adapter and preserve ToolMessage content as JSON.
- [x] Include route/tool metadata in `_stream_state` and conversation artifacts with credential redaction.
- [x] Run focused ReAct and SSE tests.

### Task 4: Add safe metadata cache and aggregate runtime counters

**Files:**
- Modify: `src/wallet_agent/observability.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/graph/build.py`
- Modify: `src/wallet_agent/main.py`
- Modify: `src/wallet_agent/config.py`
- Test: `tests/test_observability.py`
- Test: `tests/graph/test_unified_react.py`

**Interfaces:**
- `RuntimeMetrics.record_tool/record_model/snapshot/reset` are process-local and thread-safe.
- `MetadataCache.get/set` supports a bounded TTL and JSON-safe values; only asset metadata uses it by default.

- [x] Write failing tests for cache hit/expiry and runtime counter snapshots.
- [x] Implement the bounded cache and metrics registry without adding a service dependency.
- [x] Apply caching only to asset discovery and record cache hits/misses in tool timings.
- [x] Run focused observability and ReAct tests.

### Task 5: Expand offline evaluation matrix and verify demo observability

**Files:**
- Modify: `src/evals/wallet_agent_evals.py`
- Modify: `tests/evals/test_wallet_agent_evals.py`
- Modify: `demo-next/lib/agent-utils.ts`
- Modify: `demo-next/app/page.tsx`
- Modify: `demo-next/app/globals.css`
- Test: `demo-next/test/agent-utils.test.mjs`

**Interfaces:**
- Evaluation cases identify `route`, `tool_count`, `answer_scope`, `lifecycle_boundary`, and `latency_budget` dimensions.
- Demo keeps final answer separate from processing trace and displays route/model/tool timing safely.

- [x] Add failing evaluation assertions for strict route, quote handoff, all-nonzero portfolio scope, and tool timing.
- [x] Implement deterministic offline cases and a compact summary command.
- [x] Update demo artifact extraction/rendering for route and timing metadata.
- [x] Run Node tests and Next build.

### Task 6: Full verification and integration commit

- [x] Run `PYTHONPATH=. ./.venv/bin/pytest -q`.
- [x] Run `PYTHONPATH=. ./.venv/bin/ruff check src tests` and `git diff --check`.
- [x] Run Node 22 demo tests and `npm run build`.
- [x] Review route/lifecycle diff and confirm no credentials or live clients enter state.
- [x] Commit the P0/P1/P2 implementation with one focused message.
