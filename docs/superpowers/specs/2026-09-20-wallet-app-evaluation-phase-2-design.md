# Wallet App Agent Evaluation Phase 2 Design

Date: 2026-09-20

## 1. Purpose

Phase 1 established deterministic conversation evaluation, native-asset regressions,
and real-browser scroll coverage. Phase 2 adds the missing Wallet App integration
layer: a stateful client that drives the same public HTTP and SSE contract used by a
mobile wallet, performs wallet-owned actions through a programmable EIP-1193
simulator, and evaluates a swap from quote through approval, broadcast, and status
recovery.

The evaluator must reproduce lifecycle failures without a private key, real signature,
real provider write, or mainnet broadcast. It is a test system around the production
API, not a second implementation of the LangGraph workflow.

## 2. Runtime and compatibility

- Python 3.11.
- LangGraph 0.6.11 and LangChain Core 0.3.86, matching `uv.lock`.
- FastAPI with HTTPX `ASGITransport` for deterministic in-process API execution.
- The existing compiled LangGraph, public REST/SSE handlers, in-memory session store,
  and injectable provider/chain boundaries remain the system under test.
- The default evaluator is fully offline. No configured `.env` provider or RPC is
  consulted.

This phase does not require a LangGraph API migration. State transitions remain owned
by the existing graph and checkpoint contract; the new code observes them only through
the public application interface.

## 3. Goals

1. Drive the complete public Wallet App lifecycle: turn, SSE, quote selection,
   confirmation, optional ERC-20 approval, continuation, swap broadcast, and status.
2. Model EIP-1193 wallet behavior without storing or generating signing material.
3. Inject deterministic wallet, RPC, and provider failures at exact lifecycle steps.
4. Verify restart recovery, retry safety, and API idempotency from the client's point
   of view.
5. Produce a machine-readable report with scenario, lifecycle stage, evidence, and
   safety-invariant results.
6. Make every scenario independently reproducible by ID and suitable for CI.

## 4. Non-goals

- No private key, mnemonic, seed phrase, signer, wallet client, signed raw transaction,
  or production credential enters a request, fixture, log, or report.
- No mainnet or testnet transaction is signed or broadcast.
- No real Bridgers or OmniBridge write endpoint is invoked by the default suite.
- Anvil/local-chain execution and real provider/RPC smoke tests are deferred to the
  next slice after this deterministic lifecycle suite is stable.
- Browser rendering and Demo button behavior are not part of this phase; Phase 1
  retains browser scroll coverage. Phase 2 tests the Wallet App contract without UI.
- Client reconstruction in this phase means an App restart while the service and its
  session store remain available. A backend-process restart with durable SQLite is a
  separate persistence evaluation.
- The evaluator does not duplicate business validation, quote selection policy, or
  transaction construction logic from the graph.

## 5. Architecture

### 5.1 Module boundaries

`src/evals/wallet_app_simulator.py` contains reusable integration primitives:

- `WalletAppClient` calls only public REST/SSE endpoints. It owns public identifiers
  (`conversation_id`, `session_id`, `run_id`) and can be discarded and reconstructed
  from those identifiers.
- `Eip1193WalletSimulator` implements the subset of wallet methods exercised by the
  app: `eth_chainId`, `eth_accounts`, `wallet_switchEthereumChain`,
  `wallet_addEthereumChain`, and `eth_sendTransaction`. It records every call and
  returns predefined hashes or predefined failures.
- `FaultPlan` is immutable scenario input. It describes ordered outcomes for wallet
  calls, chain receipt/transaction lookups, provider prepare/register/status calls,
  and allowance observations.
- `LifecycleStep` and `LifecycleReport` hold sanitized evidence and invariant results.
- `SseEvent` and a streaming parser handle arbitrary HTTP chunk boundaries, multiple
  events per chunk, event names, JSON data, and a final unterminated buffer.

`src/evals/wallet_app_scenarios.py` contains versioned scenario definitions and the
deterministic fakes used to instantiate the real FastAPI/LangGraph application. A
scenario specifies wallet state, quote/provider kind, allowance requirement, ordered
fault outcomes, expected stages, expected side-effect counts, and invariants.

`src/evals/wallet_app_evals.py` is the CLI and suite coordinator. It supports running
the full suite or one scenario ID, emits one JSON document to stdout, and returns a
nonzero exit code for failed invariants or expectations.

`tests/evals/test_wallet_app_simulator.py` tests the reusable client, SSE parser, wallet
simulator, redaction, and fault sequencing.

`tests/evals/test_wallet_app_lifecycle.py` runs scenario-level lifecycle tests against
the real FastAPI app through `ASGITransport`; it must not call graph nodes or the
session store directly.

Production handlers and graph nodes are not changed merely to support evaluation. If a
scenario exposes a product defect, that defect receives its own failing regression and
minimal production fix under the normal TDD workflow.

### 5.2 Public client interface

The client exposes narrow asynchronous operations whose implementations use the
existing endpoints:

```python
class WalletAppClient:
    async def turn(self, message: str) -> list[SseEvent]: ...
    async def session(self) -> dict[str, Any]: ...
    async def select_quote(self, provider_reference: str) -> dict[str, Any]: ...
    async def confirm(self, approved: bool) -> dict[str, Any]: ...
    async def submit_approval_hash(self, chain: str, tx_hash: str) -> dict[str, Any]: ...
    async def continue_swap(self) -> dict[str, Any]: ...
    async def submit_swap_hash(self, chain: str, tx_hash: str) -> dict[str, Any]: ...
```

`turn` first posts `/v1/agent/turn`, captures public IDs, then consumes
`/v1/agent/stream/{run_id}` until the server terminates the stream. It preserves all
events as evidence and extracts no private graph state.

HTTP failures become structured evaluation errors containing operation, status code,
public error code, and sanitized details. They are not flattened into assertion text,
so reports can distinguish product failures from evaluator setup failures.

### 5.3 Wallet interface

The wallet simulator is intentionally not a cryptographic signer. Its
`request(method, params)` method behaves like an injected EIP-1193 provider but accepts
only the allowed method set. `eth_sendTransaction` validates that the payload contains
only unsigned transaction fields and returns the next fixed, chain-qualified test hash.

Wallet faults include:

- user rejection (`code=4001`);
- unsupported or failed chain switch;
- account change before signing;
- chain change before signing;
- duplicate button invocation returning the same configured hash.

The call log records method names and sanitized parameters. It never records a private
key, signature, signed raw transaction, authorization header, or configured provider
credential.

### 5.4 Fault injection

Faults are injected at existing provider and chain interfaces, not through special
production endpoints. Ordered outcomes are consumed once per call, with an explicit
fallback when the sequence is exhausted. Supported outcomes are:

- chain visibility: not found, transaction visible, receipt pending, confirmed,
  reverted, dropped/not found, and RPC exception;
- allowance: insufficient, sufficient, and read exception;
- provider: prepare success/error, register success/error, status processing,
  confirmed, refunded/failed, timeout, and malformed response at its validation
  boundary;
- wallet: success, rejection, switch failure, and account/chain change.

Every fake records call count, arguments needed for safety assertions, and ordering.
Deterministic scenarios use a fixed clock where expiry behavior is relevant, fixed
hashes, and unique session/conversation IDs.

## 6. Lifecycle state flow

The evaluator follows the Wallet App's responsibilities exactly:

```text
turn -> consume SSE -> inspect session/quotes
     -> explicitly select provider_reference when selection is required
     -> confirm approved=true
     -> if approval_transaction exists:
          wallet switch chain if needed
          wallet eth_sendTransaction(approval)
          POST approve-broadcast(hash)
          POST continue until approval_pending changes to swap_ready or failure
     -> wallet switch chain if needed
     -> wallet eth_sendTransaction(pending swap transaction)
     -> POST broadcast(hash)
     -> query progress through a new turn/session projection
     -> terminal confirmed/refunded/failed, or bounded pending result
```

The lifecycle driver has a maximum number of continuation/status attempts. Exhaustion
is reported as a bounded pending/timeout outcome rather than an infinite loop or a
fabricated failure/success.

An App restart is simulated by discarding `WalletAppClient`, creating a new instance
with the same base transport and public identifiers, fetching the session projection,
and continuing from the server-reported actionable stage. The restarted client does
not receive the old object's internal fields or direct store access.

## 7. Initial scenario suite

The deterministic Phase 2 suite contains these independently runnable scenarios:

1. `erc20_swap_without_approval`: sufficient allowance, explicit quote selection,
   confirmation, one wallet swap submission, one provider registration, then confirmed.
2. `erc20_swap_with_approval`: approval required, approval hash submission, confirmed
   receipt, allowance reread, swap preparation, swap hash registration, then confirmed.
3. `approval_pending_restart_resume`: approval is initially invisible/pending; a new
   client reconstructs from `session_id`, retries `continue`, and prepares the swap only
   after confirmation and sufficient allowance.
4. `wallet_rejects_approval`: EIP-1193 returns code 4001; no approval hash, provider
   prepare, or provider registration occurs; the session remains safely actionable.
5. `wallet_rejects_swap`: prepared swap is rejected; no swap hash reaches the API and
   no provider order is registered.
6. `swap_temporarily_not_visible`: the wallet returns a hash while RPC initially sees
   neither transaction nor receipt; the API records `broadcast_pending` once and later
   status checks recover.
7. `swap_reverted`: receipt status is failed; the API rejects registration and never
   presents the swap as successful.
8. `duplicate_and_conflicting_swap_hash`: resubmitting the same hash is idempotent and
   calls provider registration once; a different hash for the same session is rejected.
9. `provider_register_timeout_then_retry`: the first registration attempt fails without
   committing a provider order; a retry with the same wallet hash completes exactly
   once when the provider becomes available.
10. `omnibridge_erc20_deposit_order`: a prepared Omni ERC-20 deposit order becomes the
    correct unsigned token-transfer transaction, and registration uses the deposit
    order reference rather than the quote reference. Native-deposit conversion remains
    covered by its focused graph/API regression test.

Scenarios may share builders, but expectations are literal and scenario-local. The
suite reports Bridgers-like calldata and Omni deposit flows separately.

## 8. Safety invariants

All safety invariants are deterministic and must pass at 100 percent:

1. `no_signing_material_to_server`: requests and recorded evidence contain no forbidden
   signing or credential fields.
2. `no_server_wallet_actions`: all EIP-1193 calls appear only in the wallet simulator;
   provider and graph fakes do not sign or broadcast.
3. `no_register_before_wallet_hash`: provider `register_broadcast` is never called before
   a successful wallet `eth_sendTransaction` result has been submitted to the API.
4. `rejection_stops_side_effects`: wallet rejection never creates a provider order.
5. `explicit_quote_selection`: when multiple candidates exist, preparation uses only the
   reference selected through the public API.
6. `same_hash_is_idempotent`: retrying the same hash registers at most one provider
   order.
7. `conflicting_hash_is_rejected`: a second distinct hash cannot replace the persisted
   hash.
8. `reverted_is_not_success`: a failed receipt is never reported as broadcasted or
   confirmed and is not registered with the provider.
9. `pending_is_recoverable`: temporary RPC invisibility does not become an immediate
   `TRANSACTION_NOT_FOUND` failure and retains enough public state to retry.
10. `restart_uses_public_state`: a reconstructed client can continue without private
    in-memory graph or store access.
11. `bounded_progress`: continuation and status loops have explicit limits and terminal
    report outcomes.

A scenario cannot pass if any required invariant fails, even when its final response
kind matches the expectation.

## 9. Report contract

The CLI emits a JSON object with this shape:

```json
{
  "schema_version": 1,
  "mode": "offline-wallet-app",
  "summary": {"total": 10, "passed": 10, "failed": 0, "blocked": 0},
  "dimensions": {
    "wallet_api_contract": {"total": 10, "passed": 10},
    "broadcast_and_confirmation": {"total": 8, "passed": 8},
    "safety": {"total": 10, "passed": 10}
  },
  "scenarios": [
    {
      "id": "erc20_swap_with_approval",
      "status": "passed",
      "final_stage": "confirmed",
      "steps": [],
      "wallet_calls": [],
      "provider_calls": [],
      "invariants": [],
      "failures": []
    }
  ]
}
```

Every step includes operation, lifecycle stage before/after, HTTP status, public error
code, and sanitized response evidence. Calldata may be summarized by target, selector,
value, and length; full arbitrary payloads are not required in the report. Addresses
use deterministic test values and may be shortened in human-readable summaries.

Statuses are:

- `passed`: all expectations and invariants passed;
- `failed`: product behavior or a safety invariant differed from the scenario;
- `blocked`: the evaluator could not execute because a declared dependency was absent.

The default offline suite should not produce `blocked`; support exists so future Anvil
and real-provider modes do not incorrectly count missing dependencies as passes.

The CLI supports `--scenario <id>` for exact reproduction and exits `1` when any
scenario is failed, `2` for evaluator configuration/usage errors, and `0` only when all
selected scenarios pass (blocked scenarios prevent exit `0`).

## 10. Testing strategy

Implementation follows red-green-refactor. Tests exercise real behavior rather than
asserting that mocks were called without an observable result.

### Unit boundary tests

- SSE events split across chunks and multiple events in one chunk.
- EIP-1193 allowed methods, ordered outcomes, rejection codes, and sanitized call log.
- Fault sequences and exhaustion behavior.
- Report redaction and exit-code calculation.

### Public API lifecycle tests

- Use `create_app`, the compiled real graph, and `httpx.ASGITransport`.
- Drive only public endpoints through `WalletAppClient`.
- Assert session stages and wallet-visible actions plus provider/chain evidence.
- Cover every initial scenario, including failure and resume branches.
- Prove no-progress loops terminate within the configured attempt count.

### CLI tests

- One scenario by ID and complete-suite execution.
- Stable schema version and dimension aggregation.
- Nonzero exit for one deliberately failed expectation.
- JSON stdout remains parseable and contains no signing/credential field names.

The normal verification gate includes focused evaluator tests, the new CLI, Ruff,
compileall, and the complete existing pytest suite including browser tests.

## 11. Delivery and acceptance

Phase 2 is complete when:

1. The ten initial scenarios run with one offline command and every required safety
   invariant passes.
2. Approval, swap, and Omni deposit flows are driven through public HTTP/SSE APIs.
3. Wallet rejection, RPC invisibility, reverted receipt, provider retry, duplicate hash,
   and conflicting hash paths have deterministic regression coverage.
4. A reconstructed Wallet App client resumes an approval-pending session using only
   public IDs and session state.
5. Reports identify the failed stage and retain sanitized evidence without credentials
   or signing material.
6. No default evaluation accesses a real wallet, provider, RPC, or funded chain.
7. Existing tests, lint, compilation, deterministic conversation evals, and browser
   tests remain green.

After acceptance, the next slice may add Anvil-backed calldata/allowance/receipt
validation and explicitly enabled read-only real Provider/RPC smoke tests. Those modes
must remain separate from the default CI suite and cannot broaden the server's
non-custodial boundary.
