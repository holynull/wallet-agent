# OKX Primary Wallet Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OKX the only wallet/market data source, add OKX Explorer transaction queries, preserve Bridgers/OmniBridge swap APIs and browser-wallet signing, and isolate the minimal RPC execution observer.

**Architecture:** Extend the existing normalized OKX client with specific balances and Explorer transaction methods. REST and Graph wallet-data paths use that adapter directly; swap authorization and wallet broadcast keep a narrow execution-observer dependency for allowance, nonce, fee, receipt, and propagation checks. No signed transaction is sent to the backend, and no CoinGecko fallback remains.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, LangGraph, httpx transports, pytest/pytest-asyncio, existing EVM RPC adapters, OKX `/api/v6` signed client.

**Spec:** `docs/superpowers/specs/2026-09-22-okx-primary-wallet-migration-design.md`

## Global Constraints

- OKX credentials, authentication headers, and signed transactions never enter REST responses, SSE events, Graph state, checkpoints, logs, or Demo debug output.
- Bridgers and OmniBridge remain the only swap quote/order providers.
- Browser wallets remain responsible for user confirmation, signing, and `eth_sendTransaction`.
- Do not use CoinGecko or add a price fallback.
- Do not guess undocumented OKX paths; use the verified paths below and return structured capability errors for unsupported chains.
- Tests use deterministic fake clients/transports and do not contact live OKX, Bridgers, OmniBridge, or RPC services.
- Preserve unrelated user changes already present in the worktree.

## File Map

- Create `src/wallet_agent/okx/explorer.py`: normalized OKX transaction history/detail/status adapter and response parsing.
- Create `tests/okx/test_explorer.py`: exact path/query, pagination, status normalization, and malformed-response tests.
- Modify `src/wallet_agent/okx/wallet.py`: add specific balance lookup and shared OKX error/cache behavior.
- Modify `src/wallet_agent/domain/models.py`: add source/history metadata to normalized transaction records and a paged history model.
- Modify `src/wallet_agent/api/app.py`: route balances, portfolio, transactions, and transaction detail/status through OKX data services; retain chain registry only for execution observation.
- Modify `src/wallet_agent/graph/nodes.py`: route wallet/portfolio/transaction-status nodes through OKX data services and leave swap authorization checks on the observer.
- Modify `src/wallet_agent/graph/state.py` or `src/wallet_agent/graph/nodes.py` only if the normalized transaction snapshot needs a new state field.
- Modify `src/wallet_agent/main.py`: instantiate and inject the Explorer adapter while keeping Bridgers/OmniBridge transports unchanged.
- Modify `src/wallet_agent/api/app.py` and `src/wallet_agent/graph/nodes.py` call sites to use a narrow execution-observer helper without changing existing fake adapter contracts.
- Create `tests/api/test_okx_wallet_data.py`: OKX-only balance/portfolio/history/detail route tests and capability errors.
- Create `tests/graph/test_okx_wallet_data.py`: Graph wallet-query, portfolio-query, and transaction-status routing tests.
- Modify `tests/okx/test_wallet.py`, `tests/api/test_api_contract.py`, `tests/graph/test_graph_paths.py`, and `tests/api/test_broadcast_visibility.py` for compatibility and regression coverage.
- Modify `README.md`, `docs/superpowers/specs/2026-09-21-okx-wallet-api-enhancement-design.md`, and any stale CoinGecko references after code is green.

### Task 1: Add normalized OKX transaction models and Explorer adapter

**Files:**
- Create: `src/wallet_agent/okx/explorer.py`
- Create: `tests/okx/test_explorer.py`
- Modify: `src/wallet_agent/domain/models.py:TransactionRecord`
- Modify: `src/wallet_agent/okx/__init__.py`

**Interfaces:**
- `OkxExplorerAdapter(client, chain_index_by_name)` consumes the existing `OkxSignedClient` interface `request(method, path, query=...)`.
- `get_transaction_history(address: str, chain: str | None = None, *, begin_ms: int | None = None, end_ms: int | None = None, cursor: str | None = None, limit: int = 20) -> TransactionHistoryPage` calls `GET /api/v6/dex/post-transaction/transactions-by-address`.
- `get_transaction_detail(chain: str, tx_hash: str) -> TransactionDetail` calls `GET /api/v6/dex/post-transaction/transaction-detail-by-txhash`.
- `get_transaction_status(chain: str, tx_hash: str) -> TransactionStatus` derives status from the detail response and returns `PENDING`, `CONFIRMED`, `FAILED`, or `UNKNOWN`.
- `get_supported_chains() -> dict[str, set[str]]` calls `GET /api/v6/explorer/transaction/supported-chains` and caches successful results.

- [ ] **Step 1: Write failing normalization tests.** Add tests asserting `TransactionRecord` accepts `source="okx"`, `history_kind="full"`, and an optional `method_id`; add a `TransactionHistoryPage` with `transactions` and `next_cursor`.
- [ ] **Step 2: Run the focused tests.** Run `pytest tests/okx/test_explorer.py -q`; expect import/model failures because the new models and adapter do not exist.
- [ ] **Step 3: Implement the models.** Add `source: Literal["okx"] | None`, `history_kind: Literal["full", "dex"] | None`, and `method_id: str | None` to `TransactionRecord`; add `TransactionHistoryPage` and `TransactionDetail` with validated status, chain, tx hash, gas, nonce, block, sender/receiver, and raw token-transfer detail fields.
- [ ] **Step 4: Write failing adapter tests.** Use a fake client and assert exact calls:
  ```python
  history = await adapter.get_transaction_history(ADDRESS, "ETH", limit=2)
  assert client.calls[0] == (
      "GET", "/api/v6/dex/post-transaction/transactions-by-address",
      {"address": ADDRESS, "chains": "1", "limit": "2"}, None,
  )
  detail = await adapter.get_transaction_detail("ETH", TX_HASH)
  assert client.calls[1][1:] == (
      "/api/v6/dex/post-transaction/transaction-detail-by-txhash",
      {"txHash": TX_HASH, "chainIndex": "1"}, None,
  )
  ```
- [ ] **Step 5: Implement exact OKX parsing.** Parse the documented `data[0].transactionList`/`cursor` shape for history and `data[0]` for detail; map `txStatus` values `1/pending`, `2/success`, `3/fail` and textual `pending/success/fail` to the domain enum; map timestamps in milliseconds and never expose the raw response as an exception message.
- [ ] **Step 6: Validate limits and capability errors.** Reject history limits outside `1..100`, unknown chain names, malformed `data`, and an empty detail result with stable `OKX_INVALID_ARGUMENT`, `OKX_CHAIN_UNSUPPORTED`, or `OKX_MALFORMED_RESPONSE` errors.
- [ ] **Step 7: Run the focused tests.** Run `pytest tests/okx/test_explorer.py tests/okx/test_wallet.py -q`; expect PASS.
- [ ] **Step 8: Commit the adapter slice.** Run `git add src/wallet_agent/okx src/wallet_agent/domain/models.py tests/okx/test_explorer.py tests/okx/test_wallet.py && git commit -m "feat: add OKX explorer transaction adapter"`.

### Task 2: Extend OKX wallet balances and wire application data routes

**Files:**
- Modify: `src/wallet_agent/okx/wallet.py`
- Modify: `src/wallet_agent/api/app.py:create_app`, `wallet_operation`, `portfolio`, `transaction_status`
- Modify: `src/wallet_agent/main.py`
- Create: `tests/api/test_okx_wallet_data.py`
- Modify: `tests/api/test_okx_wallet.py`, `tests/api/test_api_contract.py`

**Interfaces:**
- `OkxWalletAdapter.get_specific_balances(address, assets)` calls `POST /api/v6/dex/balance/token-balances-by-address` in batches of at most 20.
- `create_app(..., wallet_provider=..., explorer_provider=...)` stores `app.state.explorer_provider` without removing `chain_registry`.
- `/v1/wallet/{address}/balances` returns OKX balances with `source="okx"` and does not call `chain_registry`.
- `/v1/wallet/{address}/transactions` returns `{source: "okx", history_kind: "full", transactions: [...], cursor: ...}`.
- `/v1/transactions/{chain}/{tx_hash}` returns OKX normalized detail/status and preserves existing `chain`, `tx_hash`, `status`, `message`, and optional `receipt` fields.

- [ ] **Step 1: Write failing specific-balance tests.** Assert the exact POST body contains `address`, a list of `tokenContractAddresses`, and `excludeRiskToken="0"`; assert native assets use an empty contract address.
- [ ] **Step 2: Run the focused test.** Run `pytest tests/api/test_okx_wallet_data.py::test_specific_balance_route_uses_okx -q`; expect failure because the route/provider method is absent.
- [ ] **Step 3: Implement specific balance batching.** Add the adapter method, reuse `_token_balance`, reject more than 20 assets per request by chunking, and preserve provider=`okx` and observed timestamps.
- [ ] **Step 4: Write failing API routing tests.** Build an app with a fake OKX wallet/explorer provider and a chain registry that raises if called; assert balances, portfolio, transactions, and transaction detail/status all succeed without registry calls and include the source metadata.
- [ ] **Step 5: Wire the application.** Add `explorer_provider` injection, route wallet balances and portfolio to `wallet_provider.get_token_balances`, route history/detail/status to `explorer_provider`, and return `OKX_CAPABILITY_UNAVAILABLE` for unsupported chains or missing OKX providers instead of silently falling back to RPC.
- [ ] **Step 6: Preserve execution-only registry behavior.** Keep `chain_registry` available to broadcast visibility, gas fee fields, allowance, receipt, and nonce paths; do not change the swap provider map.
- [ ] **Step 7: Wire `main.py`.** Instantiate `OkxExplorerAdapter(okx_client, OKX_CHAIN_INDEX_BY_NAME)` when OKX is enabled and pass it to both `build_graph` and `create_app`.
- [ ] **Step 8: Run API tests.** Run `pytest tests/api/test_okx_wallet_data.py tests/api/test_okx_wallet.py tests/api/test_api_contract.py -q`; expect PASS.
- [ ] **Step 9: Commit the application slice.** Run `git add src/wallet_agent/okx/wallet.py src/wallet_agent/api/app.py src/wallet_agent/main.py tests/api/test_okx_wallet_data.py tests/api/test_okx_wallet.py tests/api/test_api_contract.py && git commit -m "feat: route wallet data through OKX"`.

### Task 3: Route Graph wallet queries through OKX and keep swap execution checks isolated

**Files:**
- Modify: `src/wallet_agent/graph/nodes.py:GraphRuntime`, wallet query nodes, portfolio query node, transaction status node
- Modify: `src/wallet_agent/main.py` graph construction
- Create: `tests/graph/test_okx_wallet_data.py`
- Modify: `tests/graph/test_graph_paths.py`, `tests/graph/test_task_memory.py`

**Interfaces:**
- `GraphRuntime` gains `explorer_provider: Any | None = None`.
- Wallet/portfolio read nodes use `runtime.wallet_provider`; transaction status uses `runtime.explorer_provider`.
- Swap authorization, preflight token/gas checks, approval receipt checks, and broadcast visibility continue to use `runtime.chains` until Task 4 introduces the observer wrapper.

- [ ] **Step 1: Write failing Graph tests.** Add fakes whose OKX methods return balances/history/detail while their chain adapter methods raise; assert `wallet_query`, `portfolio_query`, and `transaction_status` use OKX and include `provider="okx"`.
- [ ] **Step 2: Run the focused tests.** Run `pytest tests/graph/test_okx_wallet_data.py -q`; expect failure because the runtime field and node routing are absent.
- [ ] **Step 3: Add the runtime dependency.** Add `explorer_provider` to `GraphRuntime` and the `build_graph` factory signature without making existing callers provide it.
- [ ] **Step 4: Implement read-node routing.** Replace only read-data calls that currently invoke `runtime.chains` with OKX provider calls; leave transfer construction and swap authorization on the chain adapters.
- [ ] **Step 5: Normalize node errors.** Return stable `OKX_PROVIDER_ERROR`, `OKX_CHAIN_UNSUPPORTED`, or `OKX_CAPABILITY_UNAVAILABLE` agent errors; never turn an empty Explorer result into confirmed or failed.
- [ ] **Step 6: Run regression tests.** Run `pytest tests/graph/test_okx_wallet_data.py tests/graph/test_graph_paths.py tests/graph/test_task_memory.py -q`; expect PASS.
- [ ] **Step 7: Commit the Graph slice.** Run `git add src/wallet_agent/graph/nodes.py src/wallet_agent/main.py tests/graph/test_okx_wallet_data.py tests/graph/test_graph_paths.py tests/graph/test_task_memory.py && git commit -m "feat: use OKX for graph wallet reads"`.

### Task 4: Isolate the minimal execution observer

**Files:**
- Create: `src/wallet_agent/chains/execution_observer.py`
- Create: `tests/chains/test_execution_observer.py`
- Modify: `src/wallet_agent/api/app.py` broadcast/status helpers and gas route
- Modify: `src/wallet_agent/graph/nodes.py` preflight, allowance, approval, and transaction status call sites
- Modify: `tests/api/test_broadcast_visibility.py`, `tests/api/test_swap_authorization.py`, `tests/graph/test_okx_preflight.py`

**Interfaces:**
- `ExecutionObserverRegistry(registry)` exposes `for_chain(chain)`, returning the existing adapter or a structured `CHAIN_CAPABILITY_UNAVAILABLE` error.
- The observer contract is exactly `get_allowance`, `get_transaction_receipt`, `get_transaction`, `get_transaction_count`, `estimate_fee`, and `get_transaction_status`.
- No observer method is used for portfolio, balances, prices, or DEX history.

- [ ] **Step 1: Write failing isolation tests.** Assert `ExecutionObserverRegistry.for_chain("ETH")` returns the existing adapter, unsupported chains produce `CHAIN_CAPABILITY_UNAVAILABLE`, and OKX wallet data calls never reach the observer.
- [ ] **Step 2: Run the focused test.** Run `pytest tests/chains/test_execution_observer.py -q`; expect failure because the module does not exist.
- [ ] **Step 3: Implement the thin wrapper.** Add the registry wrapper with no new network behavior; delegate only the listed execution methods and preserve adapter exceptions.
- [ ] **Step 4: Replace direct registry lookups.** In app broadcast/status/gas and Graph preflight/allowance/approval/status paths, obtain the observer through the wrapper; retain existing hash validation and broadcast taxonomy.
- [ ] **Step 5: Add no-provider safety tests.** Assert observer failures produce `unknown`/`not_propagated`, never `confirmed`, and never register a Bridgers/OmniBridge order before source-chain visibility.
- [ ] **Step 6: Run execution regression tests.** Run `pytest tests/chains/test_execution_observer.py tests/api/test_broadcast_visibility.py tests/api/test_swap_authorization.py tests/graph/test_okx_preflight.py -q`; expect PASS.
- [ ] **Step 7: Commit the observer slice.** Run `git add src/wallet_agent/chains/execution_observer.py tests/chains/test_execution_observer.py src/wallet_agent/api/app.py src/wallet_agent/graph/nodes.py tests/api/test_broadcast_visibility.py tests/api/test_swap_authorization.py tests/graph/test_okx_preflight.py && git commit -m "refactor: isolate wallet execution observer"`.

### Task 5: Add cache behavior and remove stale CoinGecko references

**Files:**
- Modify: `src/wallet_agent/okx/explorer.py`
- Modify: `src/wallet_agent/config.py`, `.env.example`
- Modify: `src/wallet_agent/prices/__init__.py`, `README.md`, `docs/superpowers/specs/2026-09-21-okx-wallet-api-enhancement-design.md`, and stale tests/docs
- Create: `tests/okx/test_explorer_cache.py`

**Interfaces:**
- Explorer cache keys include method, address/tx hash, chain indexes, begin/end, cursor, and limit.
- Only successful OKX responses are cached; pending status has a short TTL and confirmed/failed detail has a bounded TTL.
- No `coingecko`, `CoinGecko`, or composite-price runtime/configuration symbol remains outside historical design records that explicitly describe the retired architecture.

- [ ] **Step 1: Write failing cache tests.** Assert repeated identical history/detail calls issue one client request, changed cursor/limit/tx hash issue new requests, and an error response is not cached.
- [ ] **Step 2: Run the focused test.** Run `pytest tests/okx/test_explorer_cache.py -q`; expect failure because Explorer caching is absent.
- [ ] **Step 3: Implement bounded TTL caching.** Reuse the existing async lock/request-coalescing pattern from `OkxPriceProvider`, add settings-backed TTL, and keep response objects immutable/normalized before caching.
- [ ] **Step 4: Remove stale runtime references.** Update README and current enhancement docs to describe OKX-only prices and structured unavailable responses; do not reintroduce CoinGecko settings or imports.
- [ ] **Step 5: Run cache and config tests.** Run `pytest tests/okx/test_explorer_cache.py tests/okx/test_config.py tests/test_config.py tests/prices/test_okx.py -q`; expect PASS.
- [ ] **Step 6: Commit the cache/docs slice.** Run `git add src/wallet_agent/okx/explorer.py src/wallet_agent/config.py .env.example src/wallet_agent/prices README.md docs tests/okx/test_explorer_cache.py tests/okx/test_config.py tests/test_config.py && git commit -m "feat: cache OKX explorer reads and retire stale fallbacks"`.

### Task 6: Demo/source metadata and full verification

**Files:**
- Modify: `demo/index.html`
- Modify: `tests/api/test_demo_page.py`, `tests/browser/test_okx_display.py`, `tests/test_demo_ui.py`
- Modify: `docs/local-demo-debugging.md`

- [ ] **Step 1: Write failing Demo tests.** Assert transaction history/detail responses render `OKX` source and `history_kind`, while debug output still redacts `OK-ACCESS-*`, credentials, and signed transaction material.
- [ ] **Step 2: Run the focused tests.** Run `pytest tests/api/test_demo_page.py tests/browser/test_okx_display.py tests/test_demo_ui.py -q`; expect failures for missing source/history labels.
- [ ] **Step 3: Implement minimal rendering changes.** Add source/history-kind labels and structured unavailable warnings without changing wallet signing, confirmation, or Bridgers/OmniBridge quote controls.
- [ ] **Step 4: Run the complete offline verification suite.** Run:
  ```bash
  .venv/bin/ruff check src tests
  git diff --check
  .venv/bin/pytest -q -m 'not browser'
  ```
  Expected: all non-browser tests pass and no diff whitespace errors.
- [ ] **Step 5: Run the browser verification suite if dependencies are available.** Run `.venv/bin/pytest -q tests/browser -m browser`; if the browser dependency is unavailable, report the exact skipped/blocked command rather than claiming success.
- [ ] **Step 6: Verify no unintended provider calls.** Run the OKX route tests with a fake chain registry that raises on data reads and the Bridgers/OmniBridge tests with their fake transports; confirm only OKX is used for wallet data and both swap providers remain callable.
- [ ] **Step 7: Commit the Demo/verification slice.** Run `git add demo tests docs/local-demo-debugging.md && git commit -m "feat: show OKX wallet data sources in demo"`.

## Plan Self-Review

- OKX balance, price, pre-transaction, Explorer history/detail/status, cache, API, Graph, Demo, and stale-document requirements each have a task.
- Bridgers, OmniBridge, browser signing, and minimal execution observation are explicitly preserved and regression-tested.
- No task requires an undocumented endpoint; Explorer paths are the documented `/api/v6/dex/post-transaction/*` paths.
- No task removes RPC before execution-observer coverage exists.
- Every task has a focused failing test, implementation step, passing command, and isolated commit.
