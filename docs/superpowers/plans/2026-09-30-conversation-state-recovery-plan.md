# Conversation State Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent stale swap state from hijacking transfer/status follow-ups, make unsupported read-only chain requests recoverable, and preserve truthful transaction lifecycle responses.

**Architecture:** Keep the existing single Supervisor and business graph. Add deterministic operation-boundary guards before active-task follow-up routing, make read-only chain arguments capability-aware, and preserve completed/provider-backed lifecycle data when a new conversational task starts. Add regression coverage at the graph and API projection boundaries.

**Tech Stack:** Python 3.11, LangGraph, FastAPI, Pydantic, pytest/pytest-asyncio, PostgreSQL/Redis Docker deployment.

**Spec:** `docs/wallet-agent-capability-roadmap.md`, section “Agent 智能化改造方案”.

## Global Constraints

- The backend must never sign, broadcast, or receive private wallet credentials.
- Read-only tools remain bounded to the existing allowlist and supported chain adapters.
- A new transfer task must not mutate a prior swap's provider order or transaction history.
- Every regression must be reproduced by a failing test before production code changes.

### Task 1: Reproduce the stale-task and read-only failures

**Files:**
- Modify: `tests/graph/test_task_memory.py`
- Modify: `tests/graph/test_read_only_loop.py`

- [ ] Add a graph test where an active swap receives “不是兑换，是给自己转账” and assert the result routes to `transfer`, creates a transfer task, and does not retain swap quote/confirmation state.
- [ ] Add a read-only test where a planner proposes an unsupported chain and assert the response identifies the unsupported chain and available supported chains instead of the generic “只读查询失败”。
- [ ] Run only these tests and record the expected failures before implementation.

### Task 2: Add explicit operation-boundary routing

**Files:**
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/graph/tasks.py` only if task projection needs a narrow helper.

- [ ] Add deterministic detection for transfer language and negation of the current swap task before `_active_task_followup_intent` can claim the message as a swap follow-up.
- [ ] Route explicit transfer language to `transfer`, and mark the previous draft/quote/confirmation as superseded without deleting provider-backed lifecycle records.
- [ ] Ensure the transfer extractor receives the current message and does not inherit `swap_draft.output_amount`.
- [ ] Run the new stale-task tests and the existing graph task-memory tests.

### Task 3: Make read-only chain selection truthful and recoverable

**Files:**
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/graph/read_only.py` only if argument validation belongs in the loop helper.

- [ ] Validate planned `wallet_balance` chains against configured wallet/provider capabilities before tool execution.
- [ ] When a requested chain is unsupported, return a structured recoverable response containing the requested chain, supported chains, and a clarification prompt.
- [ ] Preserve the underlying error code (`OKX_CHAIN_UNSUPPORTED`) in the response artifact while keeping user-facing text natural.
- [ ] Run read-only and response-generation tests.

### Task 4: Preserve lifecycle history and normalize status replies

**Files:**
- Modify: `src/wallet_agent/api/app.py`
- Modify: `src/wallet_agent/graph/routes.py` if status routing needs task-scoped selection.
- Modify: `tests/api/test_api_contract.py` and/or `tests/graph/test_task_memory.py`.

- [ ] Prevent a new collecting/cancelled task from erasing an existing provider order or broadcast hash that is still queryable.
- [ ] Make status follow-ups resolve the referenced active provider order/task rather than the latest unconfirmed quote.
- [ ] Add response wording that distinguishes source-chain confirmation, provider processing, destination settlement, and unknown status.
- [ ] Add a regression test for `swap -> status -> transfer -> status of previous swap`.

### Task 5: Verify and deploy

**Files:**
- No production files beyond the tasks above.

- [ ] Run focused graph/API tests.
- [ ] Run the full backend test suite and Ruff.
- [ ] Rebuild local Docker services with the existing proxy configuration if needed.
- [ ] Run the browser suite outside the sandbox and report exact pass/fail counts.

---
