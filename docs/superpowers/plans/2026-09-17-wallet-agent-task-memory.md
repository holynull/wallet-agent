# Wallet Agent Task Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make transfer and swap understanding reliable across turns by separating task classification, capability slot extraction, durable task state, and versioned confirmation.

**Architecture:** A thin Supervisor classifies each turn, capability-specific structured extractors produce slot patches, and pure task helpers merge those patches into a canonical `active_task`. Business nodes continue to validate domain requests and execute tools; existing draft and response fields remain compatibility projections while SQLite checkpoints migrate lazily.

**Tech Stack:** Python 3.11, LangGraph 0.6.11, LangChain Core 0.3.86, LangChain OpenAI 0.3.35, Pydantic 2, FastAPI, pytest, SQLite checkpointer.

**Spec:** `docs/superpowers/specs/2026-09-17-wallet-agent-task-memory-design.md`

## Global Constraints

- Models may classify and extract user-provided fields, but may not generate calldata, decide missing fields, sign, or broadcast.
- `active_task` is JSON-safe and contains no runtime clients, callbacks, credentials, or private signing material.
- Existing `swap_draft`, `transfer_draft`, `conversation_state`, REST, SSE, and Demo fields remain compatible during migration.
- Legacy checkpoints without `active_task` hydrate on read without destructive database migration.
- Effective user slot changes increment task revision once per turn and invalidate derived transaction artifacts.
- Confirmation approval is valid only for the matching task ID, revision, and canonical payload hash.
- All production behavior changes follow RED-GREEN-REFACTOR and include graph-path or persistence coverage.
- Online model evaluation uses fake chains and providers and never signs or broadcasts.

---

### Task 1: Narrow Model Understanding Contracts

**Files:**
- Create: `src/wallet_agent/models/contracts.py`
- Modify: `src/wallet_agent/models/registry.py`
- Modify: `src/wallet_agent/models/__init__.py`
- Modify: `src/wallet_agent/main.py`
- Create: `tests/models/test_understanding.py`
- Modify: `tests/models/test_registry.py`

**Interfaces:**
- Produces `RouteDecision`, `TransferSlotPatch`, and `SwapSlotPatch` Pydantic models.
- Produces `ModelRouter.classify(request: Mapping[str, Any]) -> RouteDecision`.
- Produces `ModelRouter.extract(task_kind: str, request: Mapping[str, Any]) -> BaseModel`.
- Preserves `ModelRouter.ainvoke(request)` as a compatibility alias for `classify`.
- `build_application` constructs one JSON-mode client per output schema using the same configured model ID.

- [ ] **Step 1: Write failing schema and prompt tests**

```python
@pytest.mark.asyncio
async def test_transfer_extractor_names_every_transfer_slot_and_preserves_values():
    client = CapturingStructuredClient(
        TransferSlotPatch(
            chain="Base",
            symbol="ETH",
            amount="0.01",
            recipient="0x" + "2" * 40,
        )
    )
    router = understanding_router(transfer=client)

    patch = await router.extract(
        "transfer",
        {"message": "在 Base 给 0x" + "2" * 40 + " 转 0.01 ETH"},
    )

    assert patch.amount == "0.01"
    prompt = client.messages[0].content
    assert all(
        field in prompt
        for field in ("chain", "symbol", "token_address", "decimals", "amount", "recipient")
    )
    assert "参数不完整时仍然是 transfer" in prompt
```

Add equivalent assertions for swap fields and assert that classification output contains no slot
fields.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/models/test_understanding.py tests/models/test_registry.py
```

Expected: collection fails because `wallet_agent.models.contracts` and `ModelRouter.extract` do not
exist.

- [ ] **Step 3: Add typed contracts with field descriptions**

```python
class RouteDecision(BaseModel):
    intent: IntentName


class TransferSlotPatch(BaseModel):
    chain: str | None = Field(default=None, description="用户指定的转账网络")
    symbol: str | None = Field(default=None, description="用户要转出的资产符号")
    token_address: str | None = Field(default=None, description="用户明确提供的 Token 合约地址")
    decimals: int | None = Field(default=None, ge=0, le=255)
    amount: str | None = Field(default=None, description="人类可读转账数量，不要换算 raw amount")
    recipient: str | None = Field(default=None, description="用户明确指定的收款地址")
```

Add the swap patch fields from the spec and configure `extra="forbid"`.

- [ ] **Step 4: Implement explicit classifier and extractor prompts**

Keep `_model_input` as the classifier adapter. Add `_slot_model_input(task_kind, request)` with:

- permitted JSON keys;
- current active-task slots;
- instructions that null means absent and prior slots must not be repeated unless corrected;
- one complete and one incomplete Chinese example per transaction kind;
- an explicit ban on invented addresses, decimals, amounts, or chains.

Store separate classifier and extractor clients in `ModelRegistry`, then implement `classify` and
`extract`. Compatibility `ainvoke` must call `classify` and return a Pydantic result accepted by the
existing parser.

- [ ] **Step 5: Update production model construction**

Create three structured clients from the same `ChatOpenAI` base:

```python
classifier = client.with_structured_output(RouteDecision, method="json_mode")
transfer = client.with_structured_output(TransferSlotPatch, method="json_mode")
swap = client.with_structured_output(SwapSlotPatch, method="json_mode")
```

Do not keep the flat production `IntentOutput`; re-export a compatibility alias only if an existing
import requires it.

- [ ] **Step 7: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/pytest -q tests/models/test_understanding.py tests/models/test_registry.py tests/integration/test_deepseek.py
.venv/bin/ruff check src/wallet_agent/models src/wallet_agent/main.py tests/models
```

Expected: focused unit tests pass; DeepSeek integration remains skipped unless explicitly enabled.

- [ ] **Step 7: Commit the model-contract increment**

```bash
git add src/wallet_agent/models src/wallet_agent/main.py tests/models tests/integration/test_deepseek.py
git commit -m "refactor: split wallet intent and slot extraction"
```

---

### Task 2: Canonical Active Task State

**Files:**
- Create: `src/wallet_agent/graph/tasks.py`
- Modify: `src/wallet_agent/graph/state.py`
- Create: `tests/graph/test_tasks.py`

**Interfaces:**
- Produces `hydrate_active_task(state: Mapping[str, Any]) -> ActiveTask | None`.
- Produces `new_active_task(kind: TaskKind) -> ActiveTask`.
- Produces `merge_task_patch(task, patch, *, source="user") -> TaskMergeResult`.
- Produces `project_legacy_draft(task) -> dict[str, Any]`.
- Produces `task_invalidation(changed_slots) -> dict[str, Any]` with graph-state fields to clear.
- Adds `active_task`, `predicted_intent`, and `response_action` to `AgentState`.

- [ ] **Step 1: Write failing pure-state tests**

Cover these exact behaviors:

```python
def test_hydrates_legacy_swap_draft_without_changing_values(): ...
def test_transfer_patch_increments_revision_once_for_multiple_changes(): ...
def test_identical_patch_does_not_increment_revision(): ...
def test_swap_chain_correction_clears_resolved_asset_metadata(): ...
def test_swap_amount_correction_invalidates_quotes_confirmation_and_pending_transaction(): ...
def test_projection_keeps_legacy_transfer_field_names(): ...
```

The transfer compatibility projection must map canonical `amount` to `transfer_amount`, and swap
projection keeps `source_chain`, `destination_symbol`, and `input_amount` unchanged.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
.venv/bin/pytest -q tests/graph/test_tasks.py
```

Expected: import failure for `wallet_agent.graph.tasks`.

- [ ] **Step 3: Define typed task state**

Add `TaskKind`, `TaskStatus`, `SlotSource`, and `ActiveTask` TypedDicts to `state.py`. Add only JSON
values to task slots and keep live clients in `GraphRuntime`, never checkpoint state.

- [ ] **Step 4: Implement hydration, merge, and invalidation as pure functions**

Use one UUID only when creating a new task. Hydration uses a stable task ID derived from legacy
conversation ID and task kind so repeated reads do not create different identities. Return a
`TaskMergeResult(task, changed_slots, invalidation)` dataclass. Increment revision once if and only
if at least one non-null patch value differs.

Invalidation maps must use existing graph conventions:

```python
{
    "selected_quote": None,
    "quote_candidates": [{"__clear__": True}],
    "confirmation_state": None,
    "pending_transaction": None,
    "preflight": None,
}
```

- [ ] **Step 5: Run focused tests and verify GREEN**

```bash
.venv/bin/pytest -q tests/graph/test_tasks.py
.venv/bin/ruff check src/wallet_agent/graph/tasks.py src/wallet_agent/graph/state.py tests/graph/test_tasks.py
```

- [ ] **Step 6: Commit the state-contract increment**

```bash
git add src/wallet_agent/graph/tasks.py src/wallet_agent/graph/state.py tests/graph/test_tasks.py
git commit -m "feat: add durable active wallet tasks"
```

---

### Task 3: Integrate Active Tasks Into Graph Routing

**Files:**
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/graph/routes.py`
- Modify: `src/wallet_agent/graph/build.py`
- Modify: `tests/graph/test_graph_paths.py`
- Create: `tests/graph/test_task_memory.py`
- Modify: `tests/persistence/test_recovery.py`

**Interfaces:**
- Supervisor writes `predicted_intent`; it does not own transaction slots.
- Intent processing hydrates or creates `active_task`, calls `runtime.model.extract`, merges the
  patch, projects legacy drafts, and computes deterministic missing fields.
- `intent` remains the current execution route for compatibility; `active_task.kind` remains the
  durable task identity.
- Read-only queries execute without replacing an active transfer or swap task.

- [ ] **Step 1: Add failing compiled-graph tests**

Use a fake understanding model with separate `classify` and `extract` methods. Test:

```python
@pytest.mark.asyncio
async def test_transfer_clarification_retains_known_slots_in_active_task(): ...

@pytest.mark.asyncio
async def test_swap_follow_up_classified_as_clarification_still_merges_chain_patch(): ...

@pytest.mark.asyncio
async def test_transient_balance_query_preserves_active_swap_task(): ...

@pytest.mark.asyncio
async def test_swap_correction_invalidates_previous_quotes(): ...

@pytest.mark.asyncio
async def test_cancel_marks_active_task_cancelled_and_is_idempotent(): ...

@pytest.mark.asyncio
async def test_sqlite_restart_after_clarification_accepts_next_slot_patch(): ...
```

For the clarification follow-up, classifier outputs `clarification` while `extract("swap", ...)`
returns `source_chain="BASE"` and `destination_chain="BASE"`; final state must quote successfully.

- [ ] **Step 2: Run the focused graph tests and verify RED**

```bash
.venv/bin/pytest -q tests/graph/test_task_memory.py tests/graph/test_graph_paths.py tests/persistence/test_recovery.py
```

Expected: new tests fail because `active_task` is absent or the extractor is not called.

- [ ] **Step 3: Make Supervisor classification-only**

Change `supervisor` to call `model.classify` when available, falling back to existing `ainvoke` for
legacy fake models. Save the result to both `predicted_intent` and compatibility
`supervisor_output`. A classification exception returns retryable clarification without replacing
an existing active task.

- [ ] **Step 4: Merge capability patches before deterministic validation**

In the intent node:

1. hydrate legacy active task;
2. select task kind from an explicit transaction intent or existing active task;
3. call `extract(kind, model_request)` when available;
4. adapt old flat fake outputs to canonical patches for compatibility;
5. merge and project the task;
6. call existing `_transfer_draft_request` or `_swap_draft_request`;
7. update task status/stage/missing fields from deterministic results.

Do not let a model-produced `missing_fields` override server validation.

- [ ] **Step 5: Apply correction and cancellation invalidation**

Return invalidation fields in the same graph update as the task revision change. Cancellation runs
before classification, sets `status="cancelled"`, clears active transaction artifacts, and returns
the same result when repeated.

- [ ] **Step 6: Preserve active tasks around read-only routes**

When predicted intent is wallet, portfolio, Gas, asset, price, or transaction status, route that
operation normally while leaving active task fields untouched. Ensure response projection includes
the active task so clients can show that a transaction draft still exists.

- [ ] **Step 7: Run graph and API regression tests and verify GREEN**

```bash
.venv/bin/pytest -q tests/graph/test_task_memory.py tests/graph/test_graph_paths.py tests/persistence/test_recovery.py tests/api/test_api_contract.py tests/api/test_swap_flow.py tests/api/test_transfer_flow.py
.venv/bin/ruff check src/wallet_agent/graph tests/graph
```

- [ ] **Step 8: Commit the graph integration**

```bash
git add src/wallet_agent/graph tests/graph tests/persistence/test_recovery.py
git commit -m "feat: persist wallet task slots across turns"
```

---

### Task 4: Bind Confirmation to Task Revision

**Files:**
- Modify: `src/wallet_agent/graph/state.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `tests/graph/test_resume.py`
- Modify: `tests/persistence/test_recovery.py`

**Interfaces:**
- Produces `confirmation_payload_hash(summary: Mapping[str, Any]) -> str`.
- `ConfirmationState` adds `task_id`, `task_revision`, and `payload_hash`.
- Confirmation resume returns `CONFIRMATION_STALE` without calling `provider.prepare` when task
  identity, revision, or hash differs.

- [ ] **Step 1: Write failing confirmation-version tests**

```python
@pytest.mark.asyncio
async def test_resume_rejects_confirmation_after_amount_revision_changes():
    # interrupt at revision 1, persist a revision-2 correction, then resume approval
    # assert response.errors[0].code == "CONFIRMATION_STALE"
    # assert provider.prepare_calls == 0
```

Also assert canonical hash stability across dictionary key order and retain the existing repeated
resume idempotency test.

- [ ] **Step 2: Run resume and recovery tests and verify RED**

```bash
.venv/bin/pytest -q tests/graph/test_resume.py tests/persistence/test_recovery.py
```

Expected: stale confirmation is currently accepted or lacks version fields.

- [ ] **Step 3: Add canonical payload hashing**

Serialize only the confirmation action, user-visible summary, selected provider reference, and
unsigned transaction with `json.dumps(..., sort_keys=True, separators=(",", ":"))`; hash using
SHA-256. Never include request metadata or credentials.

- [ ] **Step 4: Validate confirmation identity before and after interrupt**

Populate identity fields in `_confirmation_snapshot`. In `confirmation_wait`, reject expired or
stale confirmation before routing to prepare. Recheck immediately before `provider.prepare` so a
resumed node cannot act on a corrected task checkpoint.

- [ ] **Step 5: Verify process-restart and replay safety**

```bash
.venv/bin/pytest -q tests/graph/test_resume.py tests/persistence/test_recovery.py tests/api/test_swap_flow.py tests/api/test_swap_authorization.py
```

Expected: all pass; repeated resume leaves `prepare_calls == 1`; stale resume leaves it at `0`.

- [ ] **Step 6: Commit the confirmation-safety increment**

```bash
git add src/wallet_agent/graph/state.py src/wallet_agent/graph/nodes.py tests/graph/test_resume.py tests/persistence/test_recovery.py
git commit -m "feat: bind wallet confirmations to task revisions"
```

---

### Task 5: Project Task State, Add Safe Observability, and Update Roadmap

**Files:**
- Create: `src/wallet_agent/observability.py`
- Modify: `src/wallet_agent/api/app.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `demo/index.html`
- Modify: `docs/wallet-agent-capability-roadmap.md`
- Modify: `docs/mobile-integration.md`
- Create: `tests/test_observability.py`
- Modify: `tests/api/test_api_contract.py`
- Modify: `tests/api/test_demo_page.py`

**Interfaces:**
- SSE update and completion states include `active_task`.
- Automatic quote-to-confirm continuation forwards `active_task`.
- Demo renders task kind, stage, revision, known slots, missing fields, and confirmation status.
- Existing clients may continue reading legacy draft fields.
- `wallet_event(node, outcome, *, duration_ms, state, error_code=None)` emits structured logs with
  masked addresses and no prompt/request payload.

- [ ] **Step 1: Write failing API and Demo tests**

Assert that the API forwards `active_task` into the automatic `swap_prepare` continuation and that
the Demo contains rendering hooks for `active_task`, `revision`, `missing_fields`, and retained
slots. Assert no UI text displays prompts, API keys, or internal model configuration. In
`test_observability.py`, use `caplog` to assert that an event contains node, task kind, revision,
duration, outcome, and error code while excluding a full wallet address and keys named `api_key`,
`authorization`, `prompt`, and `request`.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
.venv/bin/pytest -q tests/test_observability.py tests/api/test_api_contract.py tests/api/test_demo_page.py
```

- [ ] **Step 3: Update API projection and continuation**

Forward `active_task` in the second graph invocation currently used after a single-provider quote.
Include it in event projection without removing `conversation_state` or `swap_draft`. Continue to
use the same session-owned `thread_id`.

- [ ] **Step 4: Update the Demo task display**

Render a compact task section inside the existing conversation/status surface, not a nested card.
Show only user-safe slot values, stage, revision, and missing fields. Confirmation buttons remain
driven by `confirmation_state.status == "requested"`.

- [ ] **Step 5: Add structured, redacted graph events**

Implement a standard-library logging helper that accepts explicit metadata rather than arbitrary
state. Mask addresses as `0x1111...1111`, omit prompts and request bodies, and emit compact JSON.
Measure classifier and extractor calls with `perf_counter`; emit success or error events from
Supervisor and slot processing. Emit tool/prepare outcomes at their existing exception boundaries.
Do not add logs inside pure state helpers.

- [ ] **Step 6: Reconcile roadmap status with acceptance evidence**

Change transaction basics to "implemented, conversational acceptance in progress" until online
eval passes. Mark read-only ToolNodes as complete for current capabilities; mark generic multi-turn
tasks and revision-bound confirmation according to the implemented tests. Document that Supervisor
is a classifier and cannot execute signing/broadcasting actions.

- [ ] **Step 6: Run focused tests and verify GREEN**

```bash
.venv/bin/pytest -q tests/test_observability.py tests/api/test_api_contract.py tests/api/test_demo_page.py
.venv/bin/ruff check src/wallet_agent/observability.py src/wallet_agent/api tests/test_observability.py tests/api
```

- [ ] **Step 8: Commit API, observability, Demo, and docs**

```bash
git add src/wallet_agent/observability.py src/wallet_agent/graph/nodes.py src/wallet_agent/api/app.py demo/index.html docs/wallet-agent-capability-roadmap.md docs/mobile-integration.md tests/test_observability.py tests/api
git commit -m "feat: expose persistent wallet task progress"
```

---

### Task 6: Expand Evals and Run Release Verification

**Files:**
- Modify: `src/evals/wallet_agent_evals.py`
- Modify: `tests/evals/test_wallet_agent_evals.py`
- Modify: `evals/README.md`
- Modify: `README.md`

**Interfaces:**
- Existing seven cases remain stable and deterministic.
- Add cases for correction, cancellation, clarification-classified follow-up, and transient balance
  query during a swap.
- Reports continue grouping results by `balance`, `transfer`, and `swap` and recording zero signing
  and broadcast side effects.

- [ ] **Step 1: Write failing eval assertions**

Extend the expected offline report with these case IDs:

```python
{
    "swap_followup_clarification_keeps_slots",
    "swap_chain_correction_invalidates_quote",
    "swap_cancel_clears_artifacts",
    "balance_during_swap_preserves_task",
    "transfer_compact_amount",
    "swap_symbol_typo",
}
```

Every transaction case asserts `broadcast_calls == 0`; quote-only and cancelled cases also assert
`prepare_calls == 0`.

- [ ] **Step 2: Run eval tests and verify RED**

```bash
.venv/bin/pytest -q tests/evals/test_wallet_agent_evals.py
```

Expected: summary count and new case IDs fail before runner cases are added.

- [ ] **Step 3: Add deterministic cases using the real compiled graph**

Reuse one stable `thread_id` across each multi-turn case. Capture final `active_task`, response kind,
revision, quote count, prepare count, and broadcast count. The offline model output for compact and
typo cases is deterministic; the online text uses `转0.01ETH` and `usd't` respectively. Do not
weaken an expectation to match a generic clarification.

- [ ] **Step 4: Run offline eval and verify 100%**

```bash
.venv/bin/pytest -q tests/evals/test_wallet_agent_evals.py
.venv/bin/python -m evals.wallet_agent_evals
```

Expected: every deterministic case passes; signing and broadcasting remain false.

- [ ] **Step 5: Run opt-in DeepSeek eval and record the actual score**

```bash
.venv/bin/python -m evals.wallet_agent_evals --online
```

Expected: the original seven core cases pass in the recorded run. If any fail, retain the strict
expectations, report the exact field loss, and do not claim online acceptance.

- [ ] **Step 6: Run full project verification**

Run the environment-sensitive config tests from a clean directory:

```bash
cd /private/tmp
/Users/zhangleping/github.com/holynull/wallet-agent/.venv/bin/pytest -q /Users/zhangleping/github.com/holynull/wallet-agent/tests
cd /Users/zhangleping/github.com/holynull/wallet-agent
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src
git diff --check
git status --short
```

Expected: all non-integration tests pass, opt-in integration tests skip unless enabled, Ruff and
compileall exit zero, and the diff contains no database, cache, credential, or report artifacts.

- [ ] **Step 7: Commit eval and documentation updates**

```bash
git add src/evals tests/evals evals/README.md README.md
git commit -m "test: extend wallet task memory evals"
```

- [ ] **Step 8: Inspect final history and working tree**

```bash
git log --oneline -8
git status --short
```

Expected: the task commits are present and the working tree is clean.
