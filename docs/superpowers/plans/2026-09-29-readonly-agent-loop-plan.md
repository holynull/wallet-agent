# Read-Only Conversational Agent Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic reference resolution, a bounded read-only model/tool loop, and fact-safe responses without weakening wallet transaction workflows.

**Architecture:** Keep the existing supervisor and deterministic write paths. Add a fixed read-only tool registry and loop with a two-round budget, project compact referents into dialogue state, and validate all generated read responses against verified facts before returning them.

**Tech Stack:** Python 3.11, LangGraph 0.6.11, LangChain Core 0.3.86, Pydantic, pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-readonly-agent-loop-design.md`

## Global Constraints

- Read-only loop tools are exactly `wallet_balance`, `price_lookup`, `gas_estimate`, `asset_discovery`, and `transaction_status`.
- `read_loop_count` is capped at 2 for every user turn.
- The loop cannot route to transfer, swap preparation, authorization, signing, or broadcast nodes.
- All checkpoint fields must be JSON serializable.
- Facts are authoritative; model text cannot change numeric or transaction-status facts.
- Existing public history and card artifacts remain backward compatible.

---

### Task 1: Extend dialogue references and read-loop state

**Files:**
- Modify: `src/wallet_agent/graph/state.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Test: `tests/graph/test_dialogue_state.py`

**Interfaces:**
- `DialogueState.referent` accepts a compact `{kind, id, facts}` object.
- `AgentState` adds `read_loop_count`, `read_tool_calls`, and `read_facts`.
- `dialogue_snapshot(state)` projects the latest wallet, asset, transaction, or quote referent.

- [ ] Add failing tests for price, transaction, and quote referent projection.
- [ ] Run `pytest tests/graph/test_dialogue_state.py -q` and confirm the new assertions fail.
- [ ] Add typed fields and JSON-safe projection helpers.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Add deterministic reference resolution

**Files:**
- Create: `src/wallet_agent/graph/dialogue.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Test: `tests/graph/test_dialogue_routing.py`

**Interfaces:**
- `resolve_read_reference(message: str, dialogue: DialogueState | None) -> dict[str, Any] | None`.
- `resolve_quote_reference(message: str, referent: Mapping[str, Any]) -> dict[str, Any] | None`.
- Resolved references only inherit facts that were previously shown in public response artifacts.

- [ ] Add failing tests for “那 ETH 呢？”, “刚才那笔交易”, “哪个报价更划算”, and “第二个”.
- [ ] Run the focused routing tests and confirm current generic classification fails.
- [ ] Implement deterministic parsing for symbols, transaction references, and quote ordinals.
- [ ] Return an explicit ambiguous result when more than one referent matches.
- [ ] Run routing and existing graph-path tests.

### Task 3: Build the fixed read-only tool loop

**Files:**
- Create: `src/wallet_agent/graph/read_only.py`
- Modify: `src/wallet_agent/graph/state.py`
- Modify: `src/wallet_agent/graph/build.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Test: `tests/graph/test_read_only_loop.py`

**Interfaces:**
- `READ_ONLY_TOOL_NAMES` is a frozen set of the five allowed tool names.
- `read_only_agent(state) -> partial AgentState` makes one model decision.
- `read_only_tool(state) -> partial AgentState` executes only validated registered tools.
- `read_only_observe(state) -> partial AgentState` records facts and increments the loop counter.
- `route_read_only(state) -> Literal["tool", "agent", "response"]` has an explicit two-round termination rule.

- [ ] Add failing tests for one tool round, two tool rounds, loop-budget exhaustion, malformed calls, unknown tools, and tool failures.
- [ ] Run the focused test file and verify the failures are caused by missing loop behavior.
- [ ] Register only existing read provider callables and validate tool arguments before execution.
- [ ] Add graph edges for agent -> tool -> observe -> agent/response without any write-node edge.
- [ ] Run loop tests and the full graph suite.

### Task 4: Add fact consistency validation and reference-aware responses

**Files:**
- Create: `src/wallet_agent/graph/facts.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/models/registry.py`
- Test: `tests/graph/test_read_response_facts.py`

**Interfaces:**
- `validate_response_draft(draft, facts) -> AgentResponseDraft | None`.
- Validation rejects unsupported assets/chains, altered numbers, and status upgrades.
- `naturalize_read_response` passes only validated drafts and uses deterministic fallback on rejection.

- [ ] Add failing tests for invented prices, balance overflow suggestions, and pending-to-confirmed rewrites.
- [ ] Run the focused tests and confirm unsafe drafts are accepted today.
- [ ] Implement strict fact extraction and status monotonicity checks.
- [ ] Route rejected drafts to existing deterministic fallback text.
- [ ] Run response and API contract tests.

### Task 5: Add end-to-end conversational evaluations

**Files:**
- Create: `tests/evals/test_conversational_scenarios.py`
- Modify: `src/evals/wallet_app_scenarios.py`
- Modify: `docs/local-demo-debugging.md`

**Interfaces:**
- Scenarios cover asset price follow-up, transaction follow-up, quote ordinal selection, two-round loop termination, and restart-safe read state.

- [ ] Add deterministic fake providers and models for each scenario.
- [ ] Assert user-visible natural language and structured-card preservation.
- [ ] Assert write nodes and confirmation interrupts are untouched by read scenarios.
- [ ] Document how to inspect `read_loop_count`, tool calls, and referents in logs.
- [ ] Run `PYTHONPATH=. .venv/bin/pytest -q tests --ignore=tests/browser`.

### Task 6: Verification and integration

**Files:**
- No production file changes unless a test exposes a regression.

- [ ] Run `.venv/bin/ruff check src tests` and separate pre-existing lint failures from touched-file failures.
- [ ] Run `git diff --check`.
- [ ] Run graph, API, and evaluation suites.
- [ ] Run `docker compose build --no-cache` and health checks after implementation.
- [ ] Commit the implementation in focused commits.
