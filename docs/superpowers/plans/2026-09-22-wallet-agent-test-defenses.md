# Wallet Agent Test Defenses Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline-first regression suite that catches OKX, Bridgers, OmniBridge, and multi-turn Agent contract regressions before they reach the Demo, with optional credential-gated live smoke tests.

**Architecture:** Add small, reusable test helpers for redacted response-shape checks and decimal/raw-amount invariants. Replay sanitized fixtures through the existing adapter transports, then extend the existing deterministic wallet-app scenario harness with provider-partial-failure and confirmation/status cases. Keep real network checks behind the existing `integration` marker and explicit environment variables.

**Tech Stack:** Python 3.11, pytest/pytest-asyncio, httpx `MockTransport`/existing fake transports, Pydantic domain models, existing LangGraph wallet-app simulator.

**Spec:** `docs/superpowers/specs/2026-09-22-wallet-agent-test-defenses-design.md`

## Global Constraints

- Default tests are offline, deterministic, and do not require API keys, wallet addresses, or external network access.
- Fixtures contain only redacted placeholder addresses and non-sensitive values; never store secrets, signatures, or complete real transaction payloads.
- Missing or uncertain token precision must raise a stable error or be resolved from trusted metadata; never silently default ERC-20 decimals to zero.
- Tests assert normalized domain output, Graph state, SSE-visible response, and conversation history rather than HTTP 200 alone.
- Integration tests are opt-in and skip clearly when credentials/configuration are incomplete.
- Preserve existing behavior and dirty worktree changes; make only minimal production changes exposed by a failing contract test.
- Every task follows TDD: write a failing test, run it and record the failure, implement the smallest fix, rerun the focused test, then run the relevant suite.

## File Map

- Create `tests/helpers/__init__.py` and `tests/helpers/contracts.py`: shared redaction, response-shape, decimal/raw amount, address, chain, and provider/source invariant helpers.
- Create `tests/contracts/fixtures/okx/` JSON fixtures and `tests/contracts/test_okx_contracts.py`: sanitized OKX wallet, token metadata, price, and pre-transaction replay cases.
- Create `tests/contracts/fixtures/providers/` JSON fixtures and `tests/contracts/test_provider_contracts.py`: Bridgers and OmniBridge normal/empty/malformed/application-error replay cases.
- Create `tests/contracts/test_helpers.py` and `tests/contracts/test_docs_workflow.py`: helper and documentation regression tests.
- Modify `tests/evals/test_wallet_app_lifecycle.py` and `src/evals/wallet_app_scenarios.py`: add offline provider-partial-failure, confirmation/status, and response-history assertions through the existing simulator.
- Create `tests/integration/test_live_contract_smoke.py`: explicit `integration`-marked, environment-gated adapter smoke tests with secret-safe diagnostics.
- Create `docs/local-demo-debugging.md`: map fixture/contract failures to Demo debug logs and the appropriate adapter/Graph layer.
- Modify `pyproject.toml` only if a new marker is required; prefer the existing `browser` and `integration` markers and existing dependencies.

---

### Task 1: Add reusable contract and invariant helpers

**Files:**
- Create: `tests/helpers/__init__.py`
- Create: `tests/helpers/contracts.py`
- Test: `tests/contracts/test_helpers.py`

**Interfaces:**
- `assert_response_shape(payload: object, *, required_keys: tuple[str, ...] = (), list_keys: tuple[str, ...] = ()) -> None` raises `AssertionError` with only key names/types/list lengths.
- `assert_decimal_in_range(value: object, *, field: str) -> int` returns an integer decimal count in `[0, 255]` and rejects missing, booleans, non-integers, and non-finite values.
- `assert_raw_matches_human(amount: object, raw_amount: object, decimals: int, *, field: str = "amount") -> None` checks exact `Decimal` equality and rejects non-finite or inconsistent values.
- `assert_asset_identity(asset: Mapping[str, object], *, chain: str, symbol: str, address: str | None = None) -> None` verifies normalized chain/symbol and optional placeholder address.
- `assert_provider_metadata(value: Mapping[str, object], *, provider: str, required_keys: tuple[str, ...] = ()) -> None` verifies provider identity and required safe metadata keys.
- `redact_fixture(value: object) -> object` recursively replaces address-like strings and sensitive key values with deterministic placeholders; `safe_shape(value: object) -> dict[str, object]` emits endpoint-safe diagnostics only.

- [ ] **Step 1: Write failing tests for helper behavior**

```python
def test_raw_amount_requires_exact_decimal_conversion():
    assert_raw_matches_human("12.5", "12500000", 6)
    with pytest.raises(AssertionError, match="amount"):
        assert_raw_matches_human("12.5", "1250000", 6)

def test_missing_or_invalid_decimals_are_rejected():
    assert_decimal_in_range("6", field="decimals") == 6
    for value in (None, "NaN", 256, True):
        with pytest.raises(AssertionError, match="decimals"):
            assert_decimal_in_range(value, field="decimals")

def test_safe_shape_does_not_return_addresses_or_secrets():
    shape = safe_shape({"address": "0x" + "a" * 40, "apiKey": "secret", "items": [1]})
    assert shape == {"keys": ["address", "apiKey", "items"], "types": {"address": "str", "apiKey": "str", "items": "list"}, "list_lengths": {"items": 1}}
    assert "secret" not in json.dumps(shape)
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/contracts/test_helpers.py -q`

Expected: collection or import failure because `tests.helpers.contracts` does not yet exist.

- [ ] **Step 3: Implement the smallest helpers**

Use `Decimal(str(value))`, reject `Decimal.is_finite() == False`, validate integer decimals without coercing booleans, recurse through mappings/sequences for redaction, and report only sorted keys, type names, and list lengths. Do not log original values.

- [ ] **Step 4: Run focused and lint checks**

Run: `pytest tests/contracts/test_helpers.py -q` and `ruff check tests/helpers tests/contracts/test_helpers.py`

Expected: all helper tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add tests/helpers tests/contracts/test_helpers.py
git commit -m "test: add response and amount contract helpers"
```

### Task 2: Replay sanitized OKX wallet and metadata contracts

**Files:**
- Create: `tests/contracts/fixtures/okx/balances_missing_decimals.json`
- Create: `tests/contracts/fixtures/okx/token_metadata.json`
- Create: `tests/contracts/fixtures/okx/empty_and_malformed.json`
- Create: `tests/contracts/fixtures/okx/price.json`
- Create: `tests/contracts/fixtures/okx/pretransaction.json`
- Create: `tests/contracts/test_okx_contracts.py`
- Modify: `src/wallet_agent/okx/wallet.py` only if a new failing replay exposes a real gap.

**Interfaces:**
- `FixtureClient.request(method, path, *, query=None, body=None) -> dict[str, object]` returns the fixture selected by path and records sanitized call shape.
- Tests use `OkxWalletAdapter` and `OkxSignedClient` boundaries already present in `src/wallet_agent/okx`.

- [ ] **Step 1: Add failing replay tests and fixtures**

Cover: a token balance without `decimals` but with matching raw balance; metadata lookup by `chainIndex`; native balance; empty `data: []`; malformed `data: null`; non-finite `totalValue`; out-of-range metadata decimals; and application error `{ "code": "51000", "data": [] }`. Assert the missing-decimals fixture resolves to the trusted metadata value, while malformed and inconsistent values raise `OKX_MALFORMED_RESPONSE` without defaulting to zero.

```python
@pytest.mark.asyncio
async def test_realistic_missing_decimals_fixture_is_recovered_from_metadata():
    client = FixtureClient.from_directory("tests/contracts/fixtures/okx")
    balances = await OkxWalletAdapter(client, {"ETH": "1"}).get_token_balances("0x" + "1" * 40, ["ETH"])
    assert balances[0].asset.decimals == 6
    assert balances[0].raw_balance == "12500000"
```

- [ ] **Step 2: Run focused tests to verify the contract gaps**

Run: `pytest tests/contracts/test_okx_contracts.py -q`

Expected: new malformed/invariant assertions fail before implementation changes; record the exact stable error code or missing normalization behavior.

- [ ] **Step 3: Implement only required adapter fixes**

Keep the existing metadata cache. Normalize OKX aliases (`tokenAssets`/`tokens`/`assets`, `tokenContractAddress`/`contractAddress`, `decimals`/`tokenDecimal`), reject `None`/non-list data, reject non-finite numbers, and allow inferred decimals only when `balance * 10**decimals == rawBalance` exactly. Preserve `OKX_MALFORMED_RESPONSE` and secret-safe details.

- [ ] **Step 4: Run the OKX contract and existing suites**

Run: `pytest tests/contracts/test_okx_contracts.py tests/okx tests/api/test_okx_wallet.py -q` and `ruff check src/wallet_agent/okx tests/contracts/test_okx_contracts.py`.

Expected: focused and existing OKX tests pass; no fixture payload is printed on failure.

- [ ] **Step 5: Commit**

```bash
git add tests/contracts/fixtures/okx tests/contracts/test_okx_contracts.py src/wallet_agent/okx/wallet.py
git commit -m "test: replay sanitized OKX wallet contracts"
```

### Task 3: Replay Bridgers and OmniBridge provider contracts

**Files:**
- Create: `tests/contracts/fixtures/providers/bridgers_quote.json`
- Create: `tests/contracts/fixtures/providers/bridgers_empty.json`
- Create: `tests/contracts/fixtures/providers/bridgers_error.json`
- Create: `tests/contracts/fixtures/providers/omnibridge_quote.json`
- Create: `tests/contracts/fixtures/providers/omnibridge_empty.json`
- Create: `tests/contracts/fixtures/providers/omnibridge_error.json`
- Create: `tests/contracts/test_provider_contracts.py`
- Modify: `src/wallet_agent/providers/bridgers.py` and/or `src/wallet_agent/providers/omnibridge.py` only when a failing contract test identifies a regression.

**Interfaces:**
- Reuse the existing provider fake transport call signature `post(path, payload, *, idempotency_key=None) -> dict[str, object]`.
- Provider tests consume `Asset`, `SwapQuoteRequest`, `NormalizedQuote`, and `ProviderResponseError` from existing domain/provider modules.

- [ ] **Step 1: Write failing provider replay tests**

Cover Bridgers `resCode=100` quote conversion, missing `txData`, retryable/permanent application codes, null/empty catalog handling, and exact input/output raw amounts. Cover OmniBridge `resCode=800` quote and order creation, empty coin list, non-800 error, invalid rate/fee, and provider metadata (`source_flag`, `source_type`, `deposit_min`, `deposit_max`). Assert successful quotes retain `provider in {"bridgers", "omnibridge"}` and that a failed Bridgers quote does not erase a successful OmniBridge candidate in the multi-provider aggregation fixture.

- [ ] **Step 2: Run focused tests to verify failures**

Run: `pytest tests/contracts/test_provider_contracts.py -q`

Expected: new edge-case tests fail with the current adapter behavior where the fixture demonstrates a contract gap.

- [ ] **Step 3: Implement minimal provider normalization/error fixes**

Preserve each provider's documented success code (`100` for Bridgers, `800` for OmniBridge), map empty list/object responses to an empty asset result only where the adapter contract permits it, raise `ProviderResponseError` for malformed quote data, reject non-finite/negative rates and amounts, and retain successful candidates when another provider returns an application error.

- [ ] **Step 4: Run provider tests and lint**

Run: `pytest tests/contracts/test_provider_contracts.py tests/providers -q` and `ruff check src/wallet_agent/providers tests/contracts/test_provider_contracts.py`.

Expected: all provider contract and existing provider tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/contracts/fixtures/providers tests/contracts/test_provider_contracts.py src/wallet_agent/providers
git commit -m "test: replay provider response contracts"
```

### Task 4: Extend offline Agent multi-turn scenario defenses

**Files:**
- Modify: `src/evals/wallet_app_scenarios.py`
- Modify: `tests/evals/test_wallet_app_lifecycle.py`
- Modify: `src/evals/wallet_app_evals.py` only if report dimensions or scenario aggregation need a stable field.

**Interfaces:**
- Extend `ScenarioDefinition` with explicit expected assistant response/history assertions while preserving existing constructor compatibility.
- Keep `run_scenario()`, `run_definition()`, `drive_scenario()`, `LifecycleReport`, and existing public HTTP simulator interfaces unchanged.

- [ ] **Step 1: Add failing scenario assertions**

Add deterministic scenarios for: (a) confirmation approved followed by status turns that must report submitted/pending, never “not confirmed”; (b) one provider quote failure plus one successful quote; (c) repeated status query that must not duplicate the same assistant message; and (d) SSE stream with intermediate events whose final visible response is retained in the session/history. Assert route/intent/task stage, final `response.kind`/message, assistant history count, and provider candidate list.

```python
@pytest.mark.asyncio
async def test_approved_swap_status_never_regresses_to_unconfirmed():
    report = await run_scenario("approved_swap_then_status")
    assert report.status == "passed"
    status_messages = [
        step.evidence.get("assistant_message", "")
        for step in report.steps
        if step.operation == "status_turn"
    ]
    assert status_messages and all("未确认" not in message for message in status_messages)
```

- [ ] **Step 2: Run the new scenario tests to verify failure**

Run: `pytest tests/evals/test_wallet_app_lifecycle.py -k "status or provider or response" -q`

Expected: the new scenarios fail until the simulator/Graph response-history assertions are wired.

- [ ] **Step 3: Implement minimal scenario/fake event changes**

Add only deterministic fake events and invariant checks. Ensure a completed turn selects the final non-null response, writes the same user-visible message to `conversation_history`, and does not append duplicates for identical status snapshots. Model provider partial failure as an error event for one candidate plus a valid quote for another.

- [ ] **Step 4: Run the full offline evaluation suite**

Run: `pytest tests/evals tests/graph tests/api -q` and `python -m evals.wallet_app_evals`.

Expected: all existing scenarios and the new scenarios pass; the JSON report remains secret-safe and dimensions include the new coverage.

- [ ] **Step 5: Commit**

```bash
git add src/evals/wallet_app_scenarios.py src/evals/wallet_app_evals.py tests/evals/test_wallet_app_lifecycle.py
git commit -m "test: add offline multi-turn response defenses"
```

### Task 5: Add optional live contract smoke tests

**Files:**
- Create: `tests/integration/test_live_contract_smoke.py`
- Modify: `tests/integration/test_okx_contract.py` only to share a secret-safe configuration helper if duplication is unavoidable.

**Interfaces:**
- `_live_config(prefix: str, required: tuple[str, ...]) -> dict[str, str] | None` returns configuration only when `<PREFIX>_INTEGRATION=1` and all required variables are non-empty.
- Live tests use existing `OkxSignedClient`, `BridgersProvider`, and `OmniBridgeProvider` transports; no write/broadcast endpoint is called.

- [ ] **Step 1: Write skip-by-default tests**

Add one OKX read-only adapter smoke and one provider catalog/quote smoke per configured provider. With no environment variables, assert pytest reports skips, not failures. With configuration, assert response shapes, decimals range, provider identity, and elapsed-time-safe diagnostics; never include URL query secrets, API signatures, wallet addresses, or full payloads in assertion messages.

- [ ] **Step 2: Run without credentials**

Run: `pytest -m integration tests/integration/test_live_contract_smoke.py -q`

Expected: every test is explicitly skipped and no secret/config value appears in output.

- [ ] **Step 3: Implement credential-gated calls and safe failure conversion**

Use `max_attempts=1`, read-only endpoints only, bounded timeout, and `pytest.skip` for incomplete configuration. On live response errors, raise an assertion containing only endpoint, top-level keys, value types, list lengths, provider error code, and elapsed milliseconds.

- [ ] **Step 4: Run existing integration collection and lint**

Run: `pytest -m integration tests/integration -q` and `ruff check tests/integration/test_live_contract_smoke.py`.

Expected: configured tests pass or produce redacted actionable failures; unconfigured tests skip.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_live_contract_smoke.py tests/integration/test_okx_contract.py
git commit -m "test: add opt-in live contract smoke checks"
```

### Task 6: Document the regression workflow and final verification

**Files:**
- Create: `docs/local-demo-debugging.md`
- Modify: `README.md` only if it already contains the test command index; otherwise keep the workflow in the new document.

**Interfaces:**
- Document commands must match the repository: `pytest tests/contracts tests/evals -q`, `pytest -m integration -q`, `python -m evals.wallet_app_evals`, `ruff check .`, and `git diff --check`.

- [ ] **Step 1: Write documentation checks**

Add a documentation test that reads `docs/local-demo-debugging.md` and asserts it names the fixture directories, stable error codes (`OKX_MALFORMED_RESPONSE`, `ASSET_NOT_FOUND`, `PROVIDER_QUOTE_FAILED`), and the offline/integration commands without embedding secrets.

- [ ] **Step 2: Run the documentation test to verify failure**

Run: `pytest tests/contracts/test_docs_workflow.py -q`

Expected: file-not-found failure before the document exists.

- [ ] **Step 3: Write the debugging guide**

Explain how to map a failed fixture to adapter logs, Graph/SSE events, and Demo debug data; include the safe diagnostic fields and the rule that real responses must be redacted before adding fixtures.

- [ ] **Step 4: Run the complete verification matrix**

Run:

```bash
pytest tests/contracts tests/okx tests/providers tests/evals tests/graph tests/api -q
pytest -m "not browser" -q
pytest -m browser -q
ruff check .
git diff --check
```

Expected: all non-browser tests pass, browser tests pass where the local browser is installed, integration tests remain opt-in, Ruff is clean, and `git diff --check` is clean.

- [ ] **Step 5: Commit**

```bash
git add docs/local-demo-debugging.md tests/contracts/test_docs_workflow.py
git commit -m "docs: add contract regression debugging workflow"
```

## Self-review against the design spec

- External response replay is covered by Tasks 2 and 3 for OKX, Bridgers, and OmniBridge, including normal, empty, malformed, and application-error shapes.
- Decimal/raw amount, chain/address/symbol, provider/source, and null-result invariants are centralized in Task 1 and exercised by Tasks 2–4.
- Multi-turn route, SSE, final response, confirmation, provider partial failure, and duplicate-history cases are covered by Task 4.
- Redaction and safe diagnostics are implemented in Task 1 and required by Tasks 5–6.
- Credential-gated real interface checks are isolated in Task 5 and skip without credentials.
- Existing production code is modified only when a replay test demonstrates a contract gap; no new dependency is required.

Plan complete and saved to `docs/superpowers/plans/2026-09-22-wallet-agent-test-defenses.md`. Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per task, with review between tasks.
2. **Inline Execution** - execute tasks in this session using executing-plans with checkpoints.

Which approach?
