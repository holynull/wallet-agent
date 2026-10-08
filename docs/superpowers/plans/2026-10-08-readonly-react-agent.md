# Read-only ReAct Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Make read-only wallet requests follow a durable ReAct loop in which the model sees the original question plus every AI tool-call and ToolMessage before producing the user response, while transfer and swap writes remain deterministic and confirmation-gated.

**Architecture:** Keep the existing supervisor and capability routing as the outer graph. For read-only intents, maintain a checkpoint-safe message transcript using LangGraph's `add_messages` reducer, run a bounded model → ToolNode → model loop with an allowlisted read-only tool set, then pass the complete transcript and verified facts to response generation. High-risk transfer, approval, signing, and broadcast nodes are unchanged and cannot be entered through the read-only tool node.

**Tech Stack:** Python 3.11, LangGraph 0.6.11, LangChain Core 0.3.86, pytest, existing wallet provider adapters.

**Spec:** The confirmed target architecture is the layered ReAct design described in the conversation: ReAct for read-only queries; deterministic state machine for transfer/swap.

## Global Constraints

- Only read-only tools may be exposed to the ReAct model: `wallet_balance`, `price_lookup`, `gas_estimate`, `asset_discovery`, `transaction_status`, `market_details`, `price_history`, and `portfolio_snapshot`.
- Transfer, swap, allowance, signing, and broadcast operations remain outside the ReAct tool node and retain their explicit confirmation gates.
- Checkpoint state must remain JSON-serializable; store LangChain messages only in the existing checkpoint-compatible message field and never store live model or provider objects.
- Every ReAct cycle has a hard maximum of `READ_ONLY_MAX_ROUNDS` and rejects repeated completed tool signatures.
- Existing asset-scope behavior remains intact: an explicit BNB request returns BNB only, while an unscoped portfolio request may return all supported non-zero assets.

### Task 1: Add durable ReAct message state and transcript helpers

**Files:**
- Modify: `src/wallet_agent/graph/state.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Test: `tests/graph/test_read_only_loop.py`

**Interfaces:**
- `AgentState.react_messages` is an `Annotated[list[Any], add_messages]` field.
- Existing `read_loop_count` and `read_tool_calls` remain the bounded counter and JSON-safe call ledger.
- `react_result` carries the verified deterministic result until the model ends the ReAct loop.

- [x] **Step 1: Write failing tests** for message order, reducer-compatible append updates, and max-step/repeated-call guards.
- [x] **Step 2: Run the focused tests and verify they fail because the new state/transcript path is absent.**
- [x] **Step 3: Add `add_messages` state metadata and a verified result handoff.**
- [x] **Step 4: Run the focused tests and verify they pass.**

### Task 2: Convert the native read path to a bounded ReAct loop

**Files:**
- Modify: `src/wallet_agent/graph/nodes.py`
- Test: `tests/graph/test_read_only_loop.py`

**Interfaces:**
- `read_only_agent` appends one `AIMessage` to `react_messages` and routes to `read_only_tool` only when validated tool calls exist.
- `read_only_tool` executes only allowlisted calls through the existing `ToolNode`, appends resulting `ToolMessage` values, and returns to `read_only_agent`.
- `route_read_only` returns `agent`, `tool`, or `response` based on the last message, completion flag, repeated signatures, and hard step bound.

- [x] **Step 1: Add failing tests** proving the model is invoked again after a ToolMessage, the loop stops on a no-tool AIMessage, invalid/write tools are rejected, and repeated calls terminate.
- [x] **Step 2: Run focused tests and verify the expected failures.**
- [x] **Step 3: Implement the bounded ReAct transitions on the existing graph edges while preserving deterministic portfolio scope checks and legacy planner fallback.**
- [x] **Step 4: Run focused tests and verify all native and legacy read-only tests pass.**

### Task 3: Generate the final answer from the complete ReAct transcript

**Files:**
- Modify: `src/wallet_agent/models/registry.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Test: `tests/graph/test_read_only_loop.py`
- Test: `tests/models/test_registry.py`

**Interfaces:**
- `ModelRouter.respond(request, facts, messages=None)` accepts an optional ordered message transcript.
- `_response_model_input` includes the original user request, verified facts, and transcript when supplied.
- `naturalize_read_response` supplies `react_messages` and still validates the final draft against facts and requested asset scope.

- [x] **Step 1: Add failing tests** asserting the response model receives HumanMessage → AIMessage(tool_calls) → ToolMessage and the original question, not facts alone.
- [x] **Step 2: Run focused tests and verify they fail against the current two-argument responder contract.**
- [x] **Step 3: Implement the optional transcript parameter with backward compatibility for injected two-argument test doubles.**
- [x] **Step 4: Run focused tests and verify final response facts/scope validation remains enforced.**

### Task 4: Add resume and observability regression coverage

**Files:**
- Modify: `src/wallet_agent/graph/nodes.py`
- Test: `tests/graph/test_read_only_loop.py`
- Test: `tests/models/test_registry.py`

**Interfaces:**
- A stable `thread_id` starts each new user turn with a fresh transcript while keeping checkpoint history durable.
- Model trace entries identify ReAct model and response stages, with tool-call IDs but no prompt secrets.

- [x] **Step 1: Add a same-thread checkpoint test and extend model trace coverage.**
- [x] **Step 2: Verify a prior ToolMessage cannot satisfy the next request.**
- [x] **Step 3: Reset the reducer-backed transcript at the turn boundary and preserve existing model stage events.**
- [x] **Step 4: Run checkpoint and model trace tests.**

### Task 5: Full verification and commit

**Files:**
- No additional production files; update tests/docs only if verification exposes a required compatibility fix.

- [x] **Step 1: Run `PYTHONPATH=. .venv/bin/pytest -q`.**
- [x] **Step 2: Run Node 22 agent-utils tests, Ruff, `npm run build`, and `git diff --check`.**
- [x] **Step 3: Review the graph diff for write-tool leakage and checkpoint serialization.**
- [x] **Step 4: Commit the ReAct implementation as a separate commit.**
