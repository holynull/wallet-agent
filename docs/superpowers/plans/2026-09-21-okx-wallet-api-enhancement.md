# OKX Wallet and Price API Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional, non-custodial OKX Wallet and Market API support for balances, prices, market history, pre-transaction checks, and broadcast observation while keeping browser-wallet signing and existing swap providers unchanged.

**Architecture:** Add a signed OKX HTTP client and narrow Wallet/Market adapters behind normalized domain models. Compose OKX with the current RPC and CoinGecko providers per asset, use OKX gas-limit/simulation as preflight evidence, and classify wallet-returned transaction hashes by multi-source visibility before polling a swap provider.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, httpx, LangGraph, pytest, pytest-asyncio, Playwright/browser tests.

**Spec:** `docs/superpowers/specs/2026-09-21-okx-wallet-api-enhancement-design.md`

## Global Constraints

- Browser or mobile wallet remains the only signer; no private key, seed phrase, signer, wallet client, or signed transaction enters the backend.
- OKX DEX aggregation and cross-chain swap APIs are excluded; OmniBridge and Bridgers remain the only swap providers.
- OKX is optional. With `OKX_ENABLED=false`, existing behavior and tests continue to work without OKX credentials.
- Never log or return OKX secrets, signed headers, request bodies, or raw credential-bearing errors.
- Use only the documented `/api/v6` endpoints listed in the spec; do not infer undocumented paths from deprecated examples.
- Use `Decimal` for monetary values, timezone-aware UTC timestamps, explicit chain-index mapping, and structured provider/source errors.
- Every implementation step follows TDD: write the focused failing test, run it to observe failure, implement the smallest change, run the focused test, then run the relevant regression tests.

### Task 1: Add configuration and the signed OKX transport

**Files:**
- Create: `src/wallet_agent/okx/__init__.py`
- Create: `src/wallet_agent/okx/client.py`
- Create: `src/wallet_agent/okx/errors.py`
- Modify: `src/wallet_agent/config.py`
- Modify: `.env.example`
- Modify: `src/wallet_agent/main.py`
- Create: `tests/okx/__init__.py`
- Create: `tests/okx/test_client.py`
- Create: `tests/okx/test_config.py`

**Interfaces:**
- `OkxSignedClient.request(method: str, path: str, *, query: Mapping[str, Any] | None = None, body: Any | None = None) -> dict[str, Any]`
- `OkxClientError(code: str, message: str, *, retryable: bool = False, status_code: int | None = None)`
- `Settings.okx_enabled: bool`, `okx_base_url`, `okx_api_key`, `okx_secret_key`, `okx_passphrase`, `okx_project_id`, `okx_max_attempts`, `okx_cache_ttl_seconds`, `okx_exclude_risk_tokens`
- `build_application()` creates the client only when OKX is enabled and stores it in `application.state.okx_client`; no credentials are passed to `create_app` responses.

- [ ] **Step 1: Write failing transport tests.** In `tests/okx/test_client.py`, add tests that assert the exact prehash for a GET query and compact POST JSON body, Base64 HMAC-SHA256 output, required headers, `OK-ACCESS-PROJECT` inclusion, and that a nonzero OKX `code` raises `OkxClientError`.
- [ ] **Step 2: Run the focused tests and verify failure.** Run `pytest tests/okx/test_client.py -q`; expect import or missing-client failures.
- [ ] **Step 3: Implement canonical signing and response validation.** Serialize query parameters once in sorted deterministic order, serialize POST bodies once with compact JSON, sign `timestamp + method.upper() + request_path_with_query + body`, send the same bytes, require `code == "0"`, and redact sensitive values in exception text.
- [ ] **Step 4: Add retry behavior.** Retry only transport exceptions, HTTP 429, and HTTP 5xx up to `okx_max_attempts`, honoring `Retry-After`; never retry authentication or nonzero application codes.
- [ ] **Step 5: Add settings and factory wiring.** Extend `Settings` validation so disabled mode needs no credentials and enabled mode requires key/secret/passphrase/project ID; update `.env.example`; close the client through the existing application transport shutdown list.
- [ ] **Step 6: Run focused and configuration tests.** Run `pytest tests/okx/test_client.py tests/okx/test_config.py tests/test_config.py -q`; expect all to pass and assert no secret appears in `repr`, logs, or raised error details.
- [ ] **Step 7: Commit the transport slice.** Run `git add src/wallet_agent/okx src/wallet_agent/config.py src/wallet_agent/main.py .env.example tests/okx` and commit `feat: add signed okx api transport`.

### Task 2: Add normalized OKX wallet balance and pre-transaction models

**Files:**
- Create: `src/wallet_agent/okx/models.py`
- Create: `src/wallet_agent/okx/wallet.py`
- Modify: `src/wallet_agent/domain/models.py`
- Modify: `src/wallet_agent/domain/providers.py`
- Create: `tests/okx/test_wallet.py`
- Create: `tests/okx/test_models.py`

**Interfaces:**
- `OkxWalletAdapter(client: OkxSignedClient, chain_index_by_name: Mapping[str, str])`
- `OkxWalletAdapter.get_total_value(address: str, chain_indexes: list[str], *, asset_type: str = "0", exclude_risk_tokens: bool = True) -> WalletTotalValue`
- `OkxWalletAdapter.get_token_balances(address: str, chain_indexes: list[str], *, exclude_risk_tokens: bool = True) -> list[TokenBalance]`
- `OkxWalletAdapter.estimate_gas_limit(transaction: TransactionContext) -> GasLimitEstimate`
- `OkxWalletAdapter.simulate_transaction(transaction: TransactionContext) -> SimulationResult`
- `OkxWalletAdapter` raises stable `AgentError`-compatible failures for unknown chains, malformed responses, and provider errors.

- [ ] **Step 1: Write model and adapter contract tests.** In `tests/okx/test_models.py` and `tests/okx/test_wallet.py`, construct documented total-value, token-balance, gas-limit, and simulation payloads; assert Decimal conversion, chain mapping, risk flags, native/token asset construction, and invalid chain/limit rejection.
- [ ] **Step 2: Run the focused tests and verify failure.** Run `pytest tests/okx/test_models.py tests/okx/test_wallet.py -q`; expect missing model/adapter failures.
- [ ] **Step 3: Implement strict normalized models.** Add `WalletTotalValue`, `TokenMarketDetails`, `TransactionContext`, `GasLimitEstimate`, `SimulationResult`, and `TokenBalance` normalization without leaking raw OKX fields.
- [ ] **Step 4: Implement documented endpoints.** Map balance calls to `/api/v6/dex/balance/total-value-by-address` and `/api/v6/dex/balance/all-token-balances-by-address`; map gas-limit and simulate calls to the two `/api/v6/dex/pre-transaction/*` POST paths with exact body fields.
- [ ] **Step 5: Run wallet tests and regression models.** Run `pytest tests/okx/test_models.py tests/okx/test_wallet.py tests/domain/test_models.py tests/domain/test_contracts.py -q`.
- [ ] **Step 6: Commit the normalized wallet slice.** Commit `feat: normalize okx wallet and pretransaction data`.

### Task 3: Implement OKX current, detailed, historical, and candle prices

**Files:**
- Create: `src/wallet_agent/prices/okx.py`
- Create: `src/wallet_agent/prices/composite.py`
- Modify: `src/wallet_agent/domain/models.py`
- Modify: `src/wallet_agent/domain/providers.py`
- Modify: `src/wallet_agent/prices/__init__.py`
- Create: `tests/prices/test_okx.py`
- Create: `tests/prices/test_composite.py`

**Interfaces:**
- `OkxPriceProvider.get_prices(assets: list[Asset]) -> list[TokenPrice]`
- `OkxPriceProvider.get_market_details(assets: list[Asset]) -> list[TokenMarketDetails]`
- `OkxPriceProvider.get_historical_prices(asset: Asset, *, period: PricePeriod, begin_ms: int | None = None, end_ms: int | None = None, cursor: str | None = None, limit: int = 50) -> HistoricalPricePage`
- `OkxPriceProvider.get_candles(asset: Asset, *, bar: CandleBar, before_ms: int | None = None, after_ms: int | None = None, limit: int = 100) -> list[PriceCandle]`
- `CompositePriceProvider(primary: OkxPriceProvider | None, fallback: CoinGeckoPriceProvider | None)` implements `TokenPriceProvider` and fills missing assets per asset, preserving `provider` and `observed_at`.

- [ ] **Step 1: Write failing OKX price tests.** In `tests/prices/test_okx.py`, use `httpx.MockTransport` to assert batch bodies for `/api/v6/dex/market/price` and `/price-info`, query encoding/limits for historical prices and candles, lowercase EVM candle addresses, native-asset handling, tuple parsing, timestamp parsing, and malformed/nonzero-code failures.
- [ ] **Step 2: Run focused price tests and verify failure.** Run `pytest tests/prices/test_okx.py -q` and observe missing provider/model failures.
- [ ] **Step 3: Add typed price models and provider attribution.** Extend `TokenPrice` with `provider`, add `TokenMarketDetails`, `HistoricalPricePage`, `HistoricalPricePoint`, and `PriceCandle`, and validate documented enum/limit ranges before calling OKX.
- [ ] **Step 4: Implement current and detailed price calls.** Use POST `/api/v6/dex/market/price` for minimal snapshots and `/api/v6/dex/market/price-info` for metrics; parse empty results as unavailable rather than zero.
- [ ] **Step 5: Implement history and candles.** Use GET `/api/v6/dex/index/historical-price` and `/api/v6/dex/market/candles`, preserve cursor, parse OHLC tuple order `[ts,o,h,l,c,vol,volUsd,confirm]`, and cache by complete normalized query.
- [ ] **Step 6: Implement composite fallback tests and code.** Add tests proving OKX wins per asset, CoinGecko fills only missing assets, providers are never averaged, and OKX outage does not discard successful fallback prices; then implement locking/TTL and `last_error` reporting.
- [ ] **Step 7: Run price regression tests.** Run `pytest tests/prices tests/graph/test_prices.py tests/api/test_price_injection.py -q`.
- [ ] **Step 8: Commit the price slice.** Commit `feat: add okx market price providers`.

### Task 4: Integrate OKX balances and prices into application wiring and REST/Graph flows

**Files:**
- Modify: `src/wallet_agent/main.py`
- Modify: `src/wallet_agent/api/app.py`
- Modify: `src/wallet_agent/graph/build.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/models/registry.py`
- Modify: `src/wallet_agent/graph/state.py`
- Create: `tests/api/test_okx_wallet.py`
- Create: `tests/api/test_okx_prices.py`
- Modify: `tests/api/test_api_contract.py`
- Modify: `tests/graph/test_prices.py`

**Interfaces:**
- `create_app(..., wallet_provider: OkxWalletAdapter | None = None, price_provider: TokenPriceProvider | None = None)` stores both as `app.state.wallet_provider` and `app.state.price_provider`.
- `GET /v1/wallet/{address}/total-value?chains=ETH,BSC&asset_type=all` returns normalized total value, source, observation time, and structured provider errors.
- `GET /v1/prices/market?chain=ETH&address=...` returns detailed current metrics.
- `GET /v1/prices/history?chain=ETH&address=...&period=1h&limit=50` returns a cursor page.
- `GET /v1/prices/candles?chain=ETH&address=...&bar=1H&limit=100` returns typed candles.
- `price_query` accepts current, market-detail, history, and candle request shapes, with server-side enum/limit validation.

- [ ] **Step 1: Write failing API tests.** Add tests for OKX-enabled total-value and price endpoints, provider-disabled 503 behavior, auth enforcement, per-asset fallback metadata, cursor validation, and rejection of arbitrary paths/credentials in query or metadata.
- [ ] **Step 2: Run focused API tests and verify failure.** Run `pytest tests/api/test_okx_wallet.py tests/api/test_okx_prices.py -q`.
- [ ] **Step 3: Wire factories.** In `main.py`, instantiate `OkxSignedClient`, `OkxWalletAdapter`, `OkxPriceProvider`, and `CompositePriceProvider` from settings; append closable transports; leave disabled mode unchanged.
- [ ] **Step 4: Add REST routes and normalized response errors.** Reuse `authenticated_user`, map chain names through the explicit OKX index map, return 422 for unsupported input and 502/503 for provider availability without leaking upstream payloads.
- [ ] **Step 5: Integrate portfolio and price Graph nodes.** Use OKX token balances/total value as enrichment while direct RPC remains spending truth; route extended price requests to narrow provider methods and preserve existing simple `price_query` responses.
- [ ] **Step 6: Run API/Graph regressions.** Run `pytest tests/api tests/graph/test_prices.py tests/graph/test_graph_paths.py -q`.
- [ ] **Step 7: Commit application integration.** Commit `feat: expose okx wallet and price capabilities`.

### Task 5: Integrate OKX pre-transaction checks and fix EIP-1559 fee fallback

**Files:**
- Modify: `src/wallet_agent/chains/evm.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/domain/models.py`
- Modify: `tests/chains/test_evm.py`
- Create: `tests/graph/test_okx_preflight.py`

**Interfaces:**
- `EVMChainAdapter.estimate_fee()` must return a positive validated priority fee when the RPC returns zero and must satisfy `max_fee_per_gas >= base_fee_per_gas + max_priority_fee_per_gas`.
- Preflight responses include `gas_sources: {rpc, okx, simulation}` and optional normalized `simulation` while retaining existing `fee_estimate` and checks.

- [ ] **Step 1: Write failing fee/preflight tests.** Assert zero `eth_maxPriorityFeePerGas` triggers `eth_feeHistory` or bounded positive fallback, max fee invariant, source metadata, OKX gas-limit maximum selection, simulation failure blocking signing, and simulation unavailability becoming a warning.
- [ ] **Step 2: Run focused tests and verify failure.** Run `pytest tests/chains/test_evm.py tests/graph/test_okx_preflight.py -q`.
- [ ] **Step 3: Implement fee source selection.** Add fee-history parsing, validate nonnegative suggestions, choose a bounded positive priority fee, and preserve legacy behavior on chains without EIP-1559 fields.
- [ ] **Step 4: Implement preflight aggregation.** Call RPC and OKX checks concurrently, choose `max(rpc_gas, okx_gas_limit, simulated_gas_used)`, retain each observation/source, and block only explicit simulation failures or existing hard checks.
- [ ] **Step 5: Run all chain/preflight regressions.** Run `pytest tests/chains tests/api/test_transfer_flow.py tests/api/test_swap_authorization.py -q`.
- [ ] **Step 6: Commit the preflight slice.** Commit `fix: validate evm fee suggestions with okx preflight`.

### Task 6: Classify broadcast propagation and gate Provider polling

**Files:**
- Modify: `src/wallet_agent/api/app.py`
- Modify: `src/wallet_agent/chains/evm.py`
- Modify: `src/wallet_agent/domain/models.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `tests/api/test_swap_flow.py`
- Create: `tests/api/test_broadcast_visibility.py`

**Interfaces:**
- `_chain_broadcast_status(adapter, tx_hash, *, sender: str | None = None) -> Literal["broadcast_seen", "broadcast_pending", "not_propagated", "confirmed", "failed", "dropped_or_replaced", "unknown"]`
- Swap broadcast responses expose `broadcast_status` and no longer report a missing source transaction as Provider processing.
- Provider order polling starts only for `broadcast_seen`, `broadcast_pending`, or `confirmed` source visibility.

- [ ] **Step 1: Write failing visibility tests.** Cover receipt success/failure, visible unmined transaction, absent transaction after bounded retries, RPC observer failure, and nonce-advanced replacement classification.
- [ ] **Step 2: Run focused tests and verify failure.** Run `pytest tests/api/test_broadcast_visibility.py tests/api/test_swap_flow.py -q`.
- [ ] **Step 3: Implement status taxonomy.** Preserve existing idempotency and hash validation, add bounded observer state, and distinguish absent from pending; never infer Provider state from a missing receipt alone.
- [ ] **Step 4: Gate Provider status polling and messages.** Return a user-readable not-propagated message, retain the tx hash and source diagnostics, and only register/poll OmniBridge or Bridgers after source visibility.
- [ ] **Step 5: Run swap/status regressions.** Run `pytest tests/api/test_swap_flow.py tests/api/test_swap_authorization.py tests/graph/test_resume.py -q`.
- [ ] **Step 6: Commit broadcast handling.** Commit `fix: distinguish missing broadcast from provider pending`.

### Task 7: Update Demo price/source/status rendering and debug safety

**Files:**
- Modify: `demo/index.html`
- Modify: `tests/api/test_demo_page.py`
- Modify: `tests/browser/test_demo_scroll.py`
- Create: `tests/browser/test_okx_display.py`
- Modify: `docs/local-demo-debugging.md`

**Interfaces:**
- Demo renders `provider`, `observed_at`, market metrics, historical/candle rows, `gas_sources`, simulation warnings, and `not_propagated` status without exposing signed headers or credentials.
- Existing backend debug copy button remains functional and copies normalized OKX debug payloads.

- [ ] **Step 1: Write failing page/browser tests.** Assert OKX/CoinGecko source labels, unavailable-source warning, history/candle response rendering, not-propagated wording, provider polling suppression, and absence of `OK-ACCESS-*` values in visible debug output.
- [ ] **Step 2: Run focused browser/page tests and verify failure.** Run `pytest tests/api/test_demo_page.py tests/browser/test_okx_display.py tests/browser/test_demo_scroll.py -q`.
- [ ] **Step 3: Implement response renderers.** Extend `renderResponse`/portfolio/price/status cards with source and observation metadata; add compact history/candle tables and explicit broadcast state copy.
- [ ] **Step 4: Keep debug data safe and copyable.** Normalize backend errors before `recordDebug`, preserve the existing copy/clear controls, and ensure the UI never renders authentication headers or request bodies.
- [ ] **Step 5: Run browser regressions.** Run the focused tests plus `pytest tests/api/test_api_contract.py tests/test_demo_ui.py -q`.
- [ ] **Step 6: Commit Demo and docs.** Commit `feat: show okx price and broadcast diagnostics in demo`.

### Task 8: Add end-to-end configuration, integration coverage, and operator documentation

**Files:**
- Modify: `tests/integration/test_real_config.py`
- Create: `tests/integration/test_okx_contract.py`
- Modify: `README.md`
- Modify: `docs/rpc-endpoints.md`
- Modify: `docs/local-demo-debugging.md`
- Modify: `.env.example`

**Interfaces:**
- Live OKX tests are marked `integration` and skipped unless `OKX_INTEGRATION=1` and all credentials are present.
- Documentation lists exact `/api/v6` endpoints, required credentials, source precedence, fallback behavior, and the non-custodial signing boundary.

- [ ] **Step 1: Write contract tests.** Add an opt-in test that calls only read-only OKX price/balance/pretransaction endpoints, checks `code == "0"`, and skips by default; never submit or broadcast a transaction.
- [ ] **Step 2: Run the default integration suite.** Run `pytest tests/integration -q`; verify the new test skips without credentials and existing real-config tests still pass.
- [ ] **Step 3: Document deployment and troubleshooting.** Explain OKX environment variables, chain index mapping, API source labels, zero-priority-fee diagnostics, `not_propagated`, and how to disable OKX safely.
- [ ] **Step 4: Run the full verification suite.** Run `pytest -q` and `git diff --check`; investigate every failure before claiming completion.
- [ ] **Step 5: Commit the final integration/docs slice.** Commit `docs: document okx wallet and price integration`.

## Final verification checklist

- [ ] `pytest -q` passes with OKX disabled and no network credentials.
- [ ] Opt-in OKX integration tests pass when configured, with no write or signing endpoint invoked.
- [ ] `git diff --check` passes.
- [ ] Demo shows current price source, historical/candle data, fee sources, and not-propagated broadcast states.
- [ ] No secret, signed header, private key, or raw transaction appears in API responses, SSE events, persisted graph state, logs, or Demo debug output.
- [ ] No OKX DEX aggregation or cross-chain Provider code was added.

