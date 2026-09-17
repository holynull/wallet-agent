# Wallet Agent Task Memory and Understanding Design

## Goal

Make conversational balance, transfer, and swap workflows reliable across turns and process
restarts by separating task classification, slot extraction, deterministic validation, and
execution. The change must preserve the non-custodial boundary: models never create calldata,
sign, broadcast, or decide whether a transaction is safe to execute.

## Current Problem

The production graph already has a durable SQLite checkpointer, a stable conversation
`thread_id`, explicit confirmation interrupts, and partial draft state. The remaining failure is
not checkpoint availability. Information reaches the checkpoint only after the model returns the
correct intent and fields.

The current `IntentOutput` is one flat schema containing fields for every capability. Production
uses JSON mode, while the prompt lists intent names without defining the slot fields. Online evals
therefore show that DeepSeek can identify balance queries but often returns only `intent` for
transfer and swap requests. The graph then changes the effective intent to `clarification`, and no
useful transfer or swap draft is saved.

The capability roadmap also describes Supervisor as a final step after generic slot filling, but
the code has already introduced Supervisor while generic task state remains partial. This design
finishes that missing foundation before adding authorization scanning, reminders, scheduled
transactions, or batch execution.

## Scope

- Introduce one canonical, checkpoint-safe `active_task` contract for conversational workflows.
- Keep task identity separate from the response action and graph route.
- Split top-level classification from capability-specific slot extraction.
- Give DeepSeek explicit, small JSON-mode schemas with field descriptions and examples.
- Merge slot patches deterministically, including correction invalidation rules.
- Derive missing fields and normalized domain requests on the server.
- Bind confirmation state to a task revision and canonical payload hash.
- Preserve existing REST/SSE and Demo response fields during migration.
- Migrate legacy checkpoint drafts when they are first read.
- Update the roadmap and wallet evals to reflect actual acceptance status.

## Non-Goals

- Authorization scanning and revoke transaction support.
- Address reputation provider integration.
- Notifications, DCA, scheduled execution, and batch transactions.
- A multi-agent architecture.
- Autonomous signing or broadcasting.
- Sending full checkpoint history or an unbounded chat transcript to the model.

## Runtime Constraints

- Python 3.11.
- LangGraph 0.6.11 and LangChain Core 0.3.86.
- Pydantic models remain the validation boundary for model and domain output.
- SQLite remains the production checkpointer selected by `PERSISTENCE_URL`.
- Existing injected fake models used by graph tests remain supported through a compatibility
  adapter while tests migrate to the new understanding interface.

## Architecture

One Supervisor remains responsible for task classification. It does not extract transaction
parameters or execute tools. Capability-specific extractors return only fields observed in the
current user message. The graph merges those fields into durable task state and applies domain
rules before selecting an execution route.

```text
User turn
  -> deterministic command guard (cancel / API-forced action)
  -> Supervisor task classification
  -> choose existing or new active task
  -> capability slot extractor
  -> deterministic slot merge and normalization
  -> deterministic missing-field calculation
     -> clarification response and checkpoint
     -> read-only ToolNode
     -> transaction preflight / quote
     -> versioned confirmation interrupt
```

For an existing task, a `clarification` classification means "continue collecting the active
task" rather than "discard the task." A same-kind classification also continues it. A read-only
query may run as a transient action without destroying the active transaction task. Starting a
different transaction task replaces the prior active task only after its intent is explicit.

## Durable State Contract

`AgentState` gains `active_task`, `predicted_intent`, and `response_action`. The existing `intent`
and `route` fields remain during migration but are not the source of task identity.

```python
class ActiveTask(TypedDict, total=False):
    task_id: str
    kind: Literal["transfer", "swap"]
    status: Literal["collecting", "ready", "awaiting_confirmation", "completed", "cancelled"]
    stage: str
    revision: int
    slots: dict[str, JsonValue]
    slot_sources: dict[str, Literal["user", "wallet", "resolver", "legacy"]]
    missing_fields: list[str]
    updated_by: Literal["user", "model", "system", "resume"]
```

Ownership and merge rules:

| Field | Producer | Merge rule |
|---|---|---|
| `active_task.kind` | Supervisor/task transition | Replace only for an explicit new task |
| `active_task.slots` | Slot merge node | Patch non-null user fields; never erase implicitly |
| `slot_sources` | Slot merge/resolver | Replace per changed slot |
| `revision` | Slot merge node | Increment only when an effective slot value changes |
| `missing_fields` | Domain validator | Replace on every validation pass |
| `predicted_intent` | Supervisor | Replace each turn |
| `response_action` | Routing/response node | Replace each turn |

The model does not write `revision`, `missing_fields`, `status`, `stage`, or confirmation fields.

### Compatibility Projection

During migration, every active transfer or swap task is projected to `transfer_draft` or
`swap_draft` so existing API projection, Demo rendering, and older tests continue to work. These
legacy fields are compatibility views, not independent state.

If a checkpoint contains a legacy draft but no `active_task`, a hydration function creates an
active task with `slot_sources` set to `legacy`, revision `1`, and the appropriate task kind. No
database rewrite or destructive migration is required.

## Model Contracts

The understanding layer exposes two explicit operations:

```python
class WalletUnderstanding(Protocol):
    async def classify(self, request: Mapping[str, Any]) -> RouteDecision: ...
    async def extract(self, task_kind: str, request: Mapping[str, Any]) -> BaseModel: ...
```

`ModelRouter.ainvoke` remains as a temporary classification-compatible facade for existing callers.

### Route Decision

`RouteDecision.intent` is a `Literal` covering supported task kinds and read-only queries. It does
not contain transfer or swap slots. The classification prompt states that incomplete transaction
requests retain their transaction intent; `clarification` is reserved for greetings, unrelated
messages, or content that cannot identify a task.

### Slot Patches

Separate Pydantic models are used for transfer and swap:

```python
class TransferSlotPatch(BaseModel):
    chain: str | None = None
    symbol: str | None = None
    token_address: str | None = None
    decimals: int | None = None
    amount: str | None = None
    recipient: str | None = None


class SwapSlotPatch(BaseModel):
    source_chain: str | None = None
    destination_chain: str | None = None
    source_symbol: str | None = None
    destination_symbol: str | None = None
    input_amount: str | None = None
    source_token_address: str | None = None
    destination_token_address: str | None = None
```

Every field receives a Pydantic description. Because DeepSeek is used through JSON mode, each
extractor prompt also includes the permitted keys, mapping rules, and representative Chinese
examples. Missing values are `null`; the extractor never invents addresses, decimals, amounts, or
chains.

Wallet sender/recipient defaults, Token metadata, decimals, raw amounts, missing fields, and domain
request construction remain deterministic server responsibilities.

## Turn Processing

### New Transfer or Swap

1. Supervisor classifies the current request.
2. The graph creates an active task with a generated `task_id` and revision `0`.
3. The matching extractor returns a slot patch.
4. Wallet context supplies allowed defaults such as sender address and current chain.
5. The graph merges the patch, increments revision if values changed, and validates requirements.
6. An incomplete task returns clarification while retaining every known slot.
7. A complete transfer enters preflight; a complete swap enters asset resolution and quote lookup.

### Follow-Up Turn

Supervisor output is stored as `predicted_intent`. If it matches the active task or is
`clarification`, the graph uses the active task's extractor. The previous task kind is never
overwritten by the clarification response.

### Corrections

User-provided fields replace older values. Derived data is invalidated as follows:

- Transfer chain, symbol, or token-address change clears derived decimals, normalized request,
  preflight, confirmation, and pending transaction.
- Transfer amount or recipient change clears normalized request, preflight, confirmation, and
  pending transaction.
- Swap chain or symbol change clears the corresponding resolved address/decimals plus all quotes,
  selection, confirmation, allowance state, and pending transaction.
- Swap amount change clears raw amount, quotes, selection, confirmation, and pending transaction.

Corrections increment task revision exactly once per merged user turn, regardless of how many slots
changed.

### Cancellation

The deterministic cancellation guard runs before model classification. Cancellation marks the task
`cancelled`, clears transaction artifacts, invalidates confirmation, and retains only an auditable
task summary. Repeating cancellation is idempotent.

### Transient Read Queries

A balance, portfolio, Gas, asset, or transaction-status query can execute without replacing an
active transfer or swap task. After the read response, the transaction task remains available for
the next turn. The response explicitly indicates the active task separately from the transient
query result.

## Confirmation and Resume Contract

`ConfirmationState` adds:

```python
task_id: str
task_revision: int
payload_hash: str
```

The hash is SHA-256 over canonical JSON containing only the user-visible action summary and
unsigned transaction/provider reference. It never includes credentials.

Before accepting `Command(resume={"approved": True})`, the graph verifies:

- confirmation status is `requested`;
- confirmation has not expired;
- task ID and revision still match the active task;
- the canonical payload hash still matches;
- the operation has not already been prepared.

A stale approval returns `CONFIRMATION_STALE` and performs no provider preparation or broadcast.
Provider preparation remains idempotent under repeated resume.

## Error Handling

- Classification failure returns a retryable clarification without clearing active task state.
- Extraction failure preserves prior slots and reports `SLOT_EXTRACTION_FAILED`.
- Validation failures identify deterministic missing or invalid fields.
- Resolver ambiguity returns candidates and does not let the model choose a token address.
- Tool/provider/RPC unavailability remains distinct from zero balance, unsafe address, or failed
  transaction.
- No error path signs or broadcasts a transaction.

## Observability

Structured events record `thread_id`, node name, task kind, task revision, predicted intent, route,
duration, outcome, and error code. Logs must not include API keys, prompts, private keys, seed
phrases, signer objects, or full wallet addresses. Addresses may be masked to a short prefix and
suffix for correlation.

## Code Organization

The existing `nodes.py` is retained initially to minimize unrelated churn, but new pure contracts
and merge logic move into focused modules:

- `wallet_agent/models/contracts.py`: route and capability slot schemas.
- `wallet_agent/graph/tasks.py`: active-task hydration, merge, invalidation, and projection.
- `wallet_agent/graph/state.py`: durable TypedDict contracts only.
- `wallet_agent/models/registry.py`: classification and extraction prompts/router.
- `wallet_agent/graph/nodes.py`: graph node orchestration using those helpers.

Further decomposition of business nodes is deferred until behavior is stable.

## API and Demo Compatibility

Existing turn, stream, session, quote-selection, confirmation, and broadcast endpoints retain their
request and response shapes. Responses add `active_task` and continue returning
`conversation_state`, legacy drafts, `missing_fields`, and confirmation state.

The Demo renders active task kind, stage, retained slots, missing fields, revision, and confirmation
status. It never exposes internal prompts or sensitive model/provider configuration.

## Testing Strategy

### Pure Unit Tests

- Legacy draft hydration.
- Transfer and swap patch merge.
- Single revision increment per turn.
- Correction invalidation matrix.
- Compatibility projection.
- Confirmation payload hashing and stale-revision rejection.
- Prompt contracts include exact field names and examples.

### Compiled Graph Tests

- Complete Chinese transfer creates an unsigned transaction.
- Missing recipient retains chain, symbol, and amount.
- Complete swap resolves assets and returns a quote.
- Multi-turn swap retains initial amount and symbols when chains arrive later.
- A model `clarification` on a follow-up still updates the active task.
- Parameter correction invalidates old quote and confirmation.
- Cancellation is idempotent.
- A transient balance query preserves the active swap.
- Malformed model output preserves task state and terminates with clarification.

### Persistence and Safety Tests

- Restart after clarification preserves active task and accepts the next slot patch.
- Restart at confirmation resumes only the matching task revision.
- Repeated resume does not duplicate provider preparation.
- No eval case signs or broadcasts.

### Evals

The deterministic seven-case suite remains a required 100% gate. Online DeepSeek eval must report
balance, transfer, and swap separately and must not count generic clarification as success when
known slots were discarded. Add paraphrases for compact amounts, typo variants, corrections,
cancellation, and multi-turn chain completion. Online evaluation is opt-in because it requires
network access, but its score is recorded before and after the change.

## Roadmap Update

The roadmap records implementation and acceptance independently:

- Transaction basics: implemented; conversational acceptance pending until online transfer evals
  pass.
- Asset and fee assistant: implemented for current endpoints; extend eval coverage beyond balance.
- Read-only ToolNode phase: complete for the currently listed balance, status, Gas, asset, and quote
  tools.
- Multi-turn task phase: in progress until this design's state and correction tests pass.
- Confirmation/resume phase: foundational implementation exists; revision binding remains in
  progress.
- Supervisor phase: thin classifier exists; considered complete only after it no longer owns slot
  extraction or safety decisions.

## Acceptance Criteria

- The existing deterministic wallet eval remains 7/7.
- The focused online DeepSeek eval reaches 7/7 for the existing core cases in the recorded
  verification run.
- Transfer and swap slot state survives clarification and SQLite graph reconstruction.
- Corrections invalidate stale derived artifacts and confirmations.
- Existing REST/SSE clients continue to work with compatibility fields.
- Repeated resume never duplicates provider preparation.
- Full pytest, Ruff, compileall, and `git diff --check` pass.
- No path accepts, persists, logs, signs, or broadcasts private signing material.
