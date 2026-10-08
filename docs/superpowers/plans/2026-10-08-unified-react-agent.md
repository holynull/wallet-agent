# Unified ReAct Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Route ordinary wallet conversations through one context-aware LangGraph ReAct loop while preserving existing API lifecycle boundaries.

**Architecture:** Add a canonical reducer-backed `messages` transcript and a bounded `react_agent -> ToolNode -> react_agent` loop. Wrap existing provider/chain capabilities as JSON-safe tools, project the final assistant message into the existing response contract, and keep explicit confirmation/sign/broadcast endpoints on their deterministic paths.

**Tech Stack:** Python 3.11, LangGraph 0.6.11, LangChain Core 0.3.86, Pydantic, pytest, existing OKX/provider adapters.

**Spec:** `docs/superpowers/specs/2026-10-08-unified-react-agent-design.md`

## Global Constraints

- Ordinary natural-language routing must not call the legacy supervisor/classifier/extractor.
- Tool results must be represented as `ToolMessage` objects in the checkpoint transcript.
- Tool execution must remain JSON-safe, bounded and compatible with SSE/checkpoint persistence.
- Explicit lifecycle HTTP endpoints may continue to use deterministic graph nodes.
- Every production change must have a failing regression test before implementation.

### Task 1: Add canonical ReAct state and model facade

**Files:**
- Modify: `src/wallet_agent/graph/state.py`
- Modify: `src/wallet_agent/models/registry.py`
- Test: `tests/graph/test_unified_react.py`
- Test: `tests/models/test_registry.py`

**Interfaces:**
- Produce `AgentState.messages: Annotated[list[Any], add_messages]` and bounded `react_steps`/`react_tool_signatures` fields.
- Produce `ModelRouter.invoke_react(messages, tools, model_id=None) -> AIMessage`.

- [x] Write tests proving `invoke_react` binds tools, records the `react_agent` model role, and preserves an AI tool-call message.
- [x] Run the focused tests and observe the expected missing-field failure.
- [x] Add the state fields and model facade with the existing model registry's selected default model.
- [x] Run focused registry/state tests and confirm they pass.

### Task 2: Build the unified tool registry

**Files:**
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/graph/nodes.py` (runtime-bound tool registry)
- Test: `tests/graph/test_unified_react.py`

**Interfaces:**
- Produce `build_react_tools(runtime, state) -> list[BaseTool]` or an equivalent runtime-bound registry used by the graph.
- Every tool returns a JSON object with `ok`, `code` and `error` fields on failure.

- [x] Write tests for the complete tool-name registry and for `wallet_balance`/`price_lookup` and `get_swap_quote` using injected fake providers.
- [x] Run those tests and observe failure because the registry is not available.
- [x] Wrap existing read providers and quote/prepare/status capability functions without adding intent regexes.
- [x] Run focused tool tests and confirm all observations are JSON serializable.

### Task 3: Replace ordinary graph routing with the ReAct loop

**Files:**
- Modify: `src/wallet_agent/graph/build.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/graph/routes.py`
- Test: `tests/graph/test_unified_react.py`

**Interfaces:**
- Produce nodes `react_agent`, `react_tools`, and `finalize_response`.
- Route only on the last `AIMessage.tool_calls`; no intent classifier is called for ordinary chat input.

- [x] Write tests for direct natural-language response, one tool loop, multi-tool value calculation, short chain follow-up, max-step termination and repeated-tool guard.
- [x] Run the focused tests and observe failure because START still enters `dialogue_manager/supervisor`.
- [x] Add the conditional graph edges and turn initialization that appends the current human message while preserving checkpoint history.
- [x] Add final response projection retaining existing response fields and model trace metadata.
- [x] Run focused graph tests and confirm all new paths pass.

### Task 4: Preserve lifecycle compatibility and checkpoint/SSE behavior

**Files:**
- Modify: `src/wallet_agent/api/app.py`
- Modify: `src/wallet_agent/graph/state.py`
- Test: `tests/api/test_api_contract.py`
- Test: `tests/api/test_sse_recovery.py`

**Interfaces:**
- Normal `/v1/agent` turns use `messages`; explicit quote selection/confirmation/allowance/broadcast continue to use their existing forced inputs.
- `_stream_state` exposes the final response and model trace without serializing non-JSON LangChain objects.

- [x] Write regression tests for same-thread resume, SSE final response, and forced quote confirmation.
- [x] Run them and observe any compatibility failures.
- [x] Project/serialize the canonical transcript at API boundaries and keep legacy lifecycle input untouched.
- [x] Run focused API tests and confirm recovery behavior.

### Task 5: Remove ordinary-turn dependence on legacy classifiers and verify

**Files:**
- Modify: `src/wallet_agent/models/registry.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `tests/graph/test_dialogue_routing.py`
- Modify: `tests/graph/test_read_only_loop.py`

- [x] Add a test model that raises if `classify`, `extract`, or legacy `respond` is called during ordinary ReAct input.
- [x] Run the test and observe the failure while the old path remains active.
- [x] Gate the legacy nodes to forced lifecycle calls only and use the final ReAct AI message for ordinary responses.
- [x] Run the full Python suite, ruff, `git diff --check`, the Node demo tests, and the Next.js build.
- [x] Inspect the diff and report exact verification results before claiming completion.
