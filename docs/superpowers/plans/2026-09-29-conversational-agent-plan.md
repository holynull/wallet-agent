# Conversational Wallet Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a natural conversation layer and one response boundary without weakening deterministic wallet execution.

**Architecture:** Keep transfer/swap execution nodes and confirmation interrupts unchanged. Add compact dialogue state, route contextual follow-ups before model classification, and route all read-only success facts through one response generator with deterministic fallback.

**Tech Stack:** Python 3.11, LangGraph 0.6.11, LangChain Core 0.3.86, Pydantic structured outputs, pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-conversational-agent-design.md`

## Global Constraints

- Never let response generation call tools or create signing/broadcast data.
- Keep transfer, swap, approval, signing, and broadcast behind existing deterministic nodes and `interrupt()` boundaries.
- Preserve JSON-serializable checkpoint state and stable `thread_id` behavior.
- Use tests first for each behavior change.
- Keep legacy state fields readable during this phase.

---

### Task 1: Add compact dialogue state and result projection

**Files:**
- Modify: `src/wallet_agent/graph/state.py:104-163`
- Modify: `src/wallet_agent/graph/nodes.py:636-705`
- Test: `tests/graph/test_dialogue_state.py`

**Interfaces:**
- `DialogueState` stores `goal`, `active_topic`, `referent`, `language`, and `last_result_kind`.
- `latest_public_result(history) -> dict[str, Any] | None` returns the latest immutable card response.
- `dialogue_snapshot(state) -> DialogueState` builds a JSON-safe snapshot without duplicating task slots.

- [ ] Write tests for extracting the latest wallet, quote, and transaction result and for preserving absent referents.
- [ ] Run the focused tests and confirm they fail because the new projection is missing.
- [ ] Add the typed state and pure projection helpers.
- [ ] Run the focused tests and confirm they pass.
- [ ] Run existing state/history tests.

### Task 2: Introduce deterministic-first dialogue routing

**Files:**
- Modify: `src/wallet_agent/graph/state.py:104-163`
- Modify: `src/wallet_agent/graph/nodes.py:2900-3052`
- Modify: `src/wallet_agent/graph/build.py:66-117`
- Test: `tests/graph/test_dialogue_routing.py`

**Interfaces:**
- `dialogue_manager(state) -> partial AgentState` stores the validated dialogue snapshot and route decision.
- `DialogueDecision` has `mode`, `capability`, `referent`, and `reason`.
- Existing intent routes remain available as compatibility projections.

- [ ] Write failing tests for wallet follow-ups, transaction follow-ups, explicit cancel, and ambiguous requests.
- [ ] Run the focused tests and confirm the current generic clarification behavior fails them.
- [ ] Add deterministic follow-up routing before the existing classifier and project its decision to legacy `intent`.
- [ ] Run focused routing tests and existing graph path tests.
- [ ] Verify transaction and swap routes still reach their existing nodes.

### Task 3: Centralize read-only response generation

**Files:**
- Modify: `src/wallet_agent/graph/nodes.py:2423-2517, 3083-3116, 5212-5565, 5865-5900`
- Modify: `src/wallet_agent/models/registry.py:235-248`
- Test: `tests/graph/test_read_response_generation.py`

**Interfaces:**
- `response_facts(state) -> dict[str, Any]` provides only verified facts.
- `generate_response(state, facts) -> dict[str, Any]` returns `AgentResponseDraft` or deterministic fallback.
- Every read-only response contains `kind`, `message`, and its structured card payload.

- [ ] Write failing tests for wallet, portfolio, price, gas, asset, transaction, and swap-status messages.
- [ ] Run the focused tests and confirm successful read nodes currently omit messages or use fixed completion text.
- [ ] Route read-only terminal states through the central response generator.
- [ ] Validate model language and suggestions against facts; use deterministic fallback on model failure.
- [ ] Run response-generation, API contract, and read-path tests.

### Task 4: Preserve and bound conversational history

**Files:**
- Modify: `src/wallet_agent/api/app.py:775-798`
- Modify: `src/wallet_agent/graph/state.py:96-101`
- Test: `tests/api/test_conversation_history.py`

**Interfaces:**
- Persisted assistant entries remain `{role, content, artifacts, suggestions}`.
- Model-facing history is a bounded recent-turn projection plus `dialogue.referent`.

- [ ] Write failing tests for card restoration, duplicate assistant entries, and bounded model history.
- [ ] Run the focused tests and confirm the current unbounded append/out-of-band behavior fails the new assertions.
- [ ] Add bounded model history projection while retaining full public history for restoration.
- [ ] Run API persistence and recovery tests.

### Task 5: Evaluation and cleanup gate

**Files:**
- Create: `tests/evals/test_conversational_scenarios.py`
- Modify: `docs/local-demo-debugging.md`

- [ ] Add multi-turn scenarios for balances, prices, transaction status, gas, quotes, cancel, and resume.
- [ ] Run the full graph/API suite, formatter, linter, and type checks available in the repository.
- [ ] Review failures for behavior regressions before removing or deprecating any legacy state field.

