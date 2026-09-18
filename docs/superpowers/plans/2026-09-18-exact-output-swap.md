# Exact-Output Swap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support exact-output swap requests safely, including provider-specific reverse quoting, correct OmniBridge ERC-20 codes, and dynamic minimum-amount guidance.

**Architecture:** Keep the existing `NormalizedQuote`/prepare/confirmation pipeline. Extend swap task state with `amount_mode` and `output_amount`, add a provider-level reverse quote helper that produces a normal forward quote, and route exact-output requests through that helper before the existing quote aggregation. Omni uses algebraic inversion plus forward verification; Bridgers uses bounded forward-quote search.

**Tech Stack:** Python 3.11, Pydantic, FastAPI/LangGraph, pytest/pytest-asyncio, Decimal arithmetic, existing provider transports.

**Spec:** `docs/superpowers/specs/2026-09-18-exact-output-swap-design.md`

## Global Constraints

- Never silently treat a destination amount as a source amount.
- Reverse quoting remains read-only and must stop before order creation or signing.
- All provider amount calculations use `Decimal`; raw units are derived only with asset decimals.
- Existing exact-input behavior and confirmation boundaries must remain unchanged.
- Omni ERC-20 codes come from provider catalog semantics; do not concatenate normalized chain names.
- Every behavior change is introduced with a failing test first.

---

### Task 1: Extend swap state and extraction for exact-output requests

**Files:**
- Modify: `src/wallet_agent/models/contracts.py:62-78`
- Modify: `src/wallet_agent/graph/tasks.py` task slot/invalidation helpers
- Modify: `src/wallet_agent/graph/nodes.py:430-500, 1850-1908, 1938-2040`
- Test: `tests/graph/test_task_memory.py`

**Interfaces:**
- Produces swap draft fields `amount_mode` (`exact_in`/`exact_out`) and `output_amount`.
- Existing `input_amount` remains the exact-input/source amount.

- [ ] **Step 1: Write failing tests**

Add tests proving that `“在 Base 用 USDC 换 USDT”` followed by `“我想换 5 USDT”` stores `output_amount="5"`, `amount_mode="exact_out"`, and does not report `input_amount` as the only missing field. Add a regression test proving `“用 5 USDC 换 USDT”` still stores `input_amount="5"`, `amount_mode="exact_in"`.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `./.venv/bin/pytest -q tests/graph/test_task_memory.py -k 'target_token_amount or exact_output or exact_input'`

Expected: the new exact-output test fails because `SwapSlotPatch` has no `output_amount` and the current node deletes destination-token amounts.

- [ ] **Step 3: Implement the minimal state/extraction change**

Add the two fields to `SwapSlotPatch`; preserve them in draft projection/merge/invalidation; map destination-qualified amounts to `output_amount` and `amount_mode="exact_out"`; map source-qualified or unitless source amounts to `input_amount` and `amount_mode="exact_in"`. Keep a missing source amount only until reverse quote resolution runs.

- [ ] **Step 4: Run focused tests**

Run: `./.venv/bin/pytest -q tests/graph/test_task_memory.py -k 'target_token_amount or exact_output or exact_input'`

Expected: PASS, with the old test updated to assert the new safe exact-output state rather than immediate clarification.

- [ ] **Step 5: Commit**

```bash
git add src/wallet_agent/models/contracts.py src/wallet_agent/graph/tasks.py src/wallet_agent/graph/nodes.py tests/graph/test_task_memory.py
git commit -m "feat: represent exact-output swap requests"
```

### Task 2: Add OmniBridge reverse quote and ERC-20 coin-code mapping

**Files:**
- Modify: `src/wallet_agent/providers/omnibridge.py:20-125`
- Test: `tests/providers/test_omnibridge.py`

**Interfaces:**
- Produces `async reverse_quote(request: SwapQuoteRequest, output_amount: Decimal) -> NormalizedQuote`.
- Internal coin-code helper maps native assets and catalog/provider symbols without appending `(ETH)` to ERC-20 assets.

- [ ] **Step 1: Write failing tests**

Add a test that `quote()` sends `USDC` and `USDT(ERC20)` for ETH ERC-20 assets. Add a reverse-quote test with `instantRate="1.00069"`, `depositCoinFeeRate="0.003"`, and `chainFee="0.3897"`; assert the first calculated deposit amount, the forward verification call, and the final expected output are at least the requested output. Add a bounds test that rejects a target requiring input above `depositMax`.

- [ ] **Step 2: Run tests and verify failure**

Run: `./.venv/bin/pytest -q tests/providers/test_omnibridge.py -k 'coin_code or reverse or bounds'`

Expected: FAIL because the adapter currently concatenates chain names and has no reverse method.

- [ ] **Step 3: Implement minimal Omni behavior**

Add a coin-code helper using native detection (`ETH` with no token address) and provider-compatible ERC-20 symbols (`USDC`, `USDT(ERC20)`). Store provider codes in quote metadata. Implement reverse arithmetic with `Decimal`, enforce min/max, call the existing forward quote with the calculated source amount, and reject a verified output below the requested amount.

- [ ] **Step 4: Run provider tests**

Run: `./.venv/bin/pytest -q tests/providers/test_omnibridge.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wallet_agent/providers/omnibridge.py tests/providers/test_omnibridge.py
git commit -m "feat: add Omni exact-output quoting"
```

### Task 3: Add bounded Bridgers reverse quote

**Files:**
- Modify: `src/wallet_agent/providers/bridgers.py:90-145`
- Test: `tests/providers/test_bridgers.py`

**Interfaces:**
- Produces `async reverse_quote(request: SwapQuoteRequest, output_amount: Decimal) -> NormalizedQuote`.

- [ ] **Step 1: Write failing tests**

Add a fake-transport test with deterministic outputs based on `fromTokenAmount`; assert reverse quoting performs bounded forward quote calls and returns the smallest source input whose `expected_output` meets the target. Add an impossible-target test asserting a clear `ValueError` after the bounded search.

- [ ] **Step 2: Run tests and verify failure**

Run: `./.venv/bin/pytest -q tests/providers/test_bridgers.py -k 'reverse'`

Expected: FAIL because no reverse method exists.

- [ ] **Step 3: Implement minimal bounded search**

Use the provider quote response's `depositMin`/`depositMax` human units, perform a finite binary search with Decimal bounds, re-quote the final candidate, and return the first candidate meeting the destination target. Convert source decimals to raw integer strings without floating point.

- [ ] **Step 4: Run provider tests**

Run: `./.venv/bin/pytest -q tests/providers/test_bridgers.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wallet_agent/providers/bridgers.py tests/providers/test_bridgers.py
git commit -m "feat: add Bridgers bounded exact-output quoting"
```

### Task 4: Route graph exact-output requests and dynamic minimum guidance

**Files:**
- Modify: `src/wallet_agent/graph/routes.py:1-90`
- Modify: `src/wallet_agent/graph/nodes.py:1938-2040`
- Test: `tests/graph/test_graph_paths.py`, `tests/graph/test_task_memory.py`

**Interfaces:**
- Exact-input quote fan-out remains unchanged.
- Exact-output requests invoke provider `reverse_quote` and return ordinary normalized quote candidates.

- [ ] **Step 1: Write failing tests**

Add a graph test with fake providers implementing `reverse_quote`; assert `“换到 5 USDT”` reaches both providers, produces quote candidates, and remains at quote/confirmation stage. Add a provider-without-reverse-capability test that returns a clarification asking for source amount. Add a dynamic-minimum suggestion test asserting the suggestion uses provider bounds instead of `1 USDC`.

- [ ] **Step 2: Run tests and verify failure**

Run: `./.venv/bin/pytest -q tests/graph/test_graph_paths.py tests/graph/test_task_memory.py -k 'exact_output or minimum or reverse'`

Expected: FAIL because graph construction currently requires `input_amount` before provider fan-out and suggestions are hard-coded.

- [ ] **Step 3: Implement graph routing**

Allow exact-output drafts through asset resolution; build a provisional request only after reverse quoting computes `input_amount`; fan out reverse calls with `asyncio.gather` and preserve existing quote validation/reduction. When no reverse quote is possible, return a clarification with the exact reason. Replace the hard-coded amount suggestion with provider-derived minimums when available.

- [ ] **Step 4: Run graph tests**

Run: `./.venv/bin/pytest -q tests/graph/test_graph_paths.py tests/graph/test_task_memory.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wallet_agent/graph/routes.py src/wallet_agent/graph/nodes.py tests/graph/test_graph_paths.py tests/graph/test_task_memory.py
git commit -m "feat: route exact-output swaps through reverse quotes"
```

### Task 5: API regression, full verification, and documentation

**Files:**
- Modify: `tests/api/test_api_contract.py`
- Modify: `docs/local-demo-debugging.md` exact-output usage section

- [ ] **Step 1: Add API-level regression tests**

Cover the full `/v1/agent/turn` path for an exact-output request, assert the response contains quote candidates or a clear provider capability error, and assert no order/broadcast call occurs before confirmation.

- [ ] **Step 2: Run the API tests**

Run: `./.venv/bin/pytest -q tests/api`

Expected: PASS.

- [ ] **Step 3: Document user-visible behavior**

Document both forms: `用 8 USDC 换 USDT` and `换到 5 USDT` (the latter may display the calculated source amount and still requires confirmation). Document that provider minimums are dynamic.

- [ ] **Step 4: Run complete verification**

Run: `./.venv/bin/pytest -q` and `./.venv/bin/ruff check src tests`

Expected: exit code 0 with no test failures or lint errors.

- [ ] **Step 5: Commit**

```bash
git add tests/api docs/local-demo-debugging.md
git commit -m "test: verify exact-output swap flow"
```
