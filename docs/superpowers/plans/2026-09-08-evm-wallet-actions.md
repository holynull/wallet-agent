# EVM Wallet Actions and Resumable Swap Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add EVM native/ERC-20 transfers, explicit quote selection, ERC-20 allowance/approve gating, and CoinGecko token prices to the existing non-custodial LangGraph wallet agent.

**Architecture:** Keep one durable LangGraph thread per conversation and one business session projection per transfer/swap. Add typed domain contracts and deterministic EVM calldata builders, then route transfer and swap-authorization stages through explicit graph nodes. Expose action endpoints that resume the same checkpoint after the app broadcasts an approval transaction.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, LangGraph 0.2+, httpx, SQLite checkpoint/session persistence, pytest/pytest-asyncio, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-08-evm-wallet-actions-design.md`

## Global Constraints

- The backend never receives private keys, seed phrases, signers, or wallet clients.
- EVM is the only chain family allowed to generate transactions in this slice; TRON/Solana remain read-only.
- Tests must be network-free by default; external provider, RPC, and CoinGecko tests are integration-marked.
- Every transaction response includes chain, chain ID, `to`, `data`, `value`, and display metadata.
- Approval continuation must verify receipt success and reread on-chain allowance; client booleans are never trusted.
- Existing REST/SSE contracts and provider lifecycle behavior must remain backward compatible.

### Task 1: Add typed transfer, allowance, price, and authorization contracts

**Files:**
- Modify: `src/wallet_agent/domain/models.py`
- Modify: `src/wallet_agent/domain/providers.py`
- Test: `tests/domain/test_models.py`

**Interfaces:**
- Add `TransferRequest`, `AllowanceRequirement`, `ApprovalTransaction`, `TokenPrice`, and `SwapAuthorizationState` Pydantic models.
- Extend `NormalizedQuote` with optional `usd_input_value`, `usd_expected_output`, `price_snapshots`, and `allowance_requirement`.
- Add `TokenPriceProvider` protocol with `get_prices(assets: list[Asset]) -> list[TokenPrice]`.

- [ ] **Step 1: Write failing model tests**

```python
def test_transfer_request_normalizes_amount_and_requires_evm_addresses():
    request = TransferRequest(
        chain="BASE", sender="0x" + "1" * 40, recipient="0x" + "2" * 40,
        amount="1.25", amount_raw="1250000000000000000", token=None,
    )
    assert request.amount_raw == "1250000000000000000"

def test_allowance_requirement_is_serializable_in_normalized_quote():
    requirement = AllowanceRequirement(
        token=Asset(chain="BASE", symbol="USDC", decimals=6, address="0x" + "3" * 40),
        owner="0x" + "1" * 40, spender="0x" + "4" * 40,
        required_amount_raw="1000000", current_allowance_raw="0",
    )
    quote = NormalizedQuote(
        provider="bridgers",
        source_asset=Asset(chain="BASE", symbol="USDC", decimals=6, address="0x" + "3" * 40),
        destination_asset=Asset(chain="BSC", symbol="USDT", decimals=6, address="0x" + "5" * 40),
        input_amount="1", input_amount_raw="1000000", expected_output="0.99",
        expected_output_raw="990000", provider_reference="quote-1",
        allowance_requirement=requirement,
    )
    assert quote.model_dump(mode="json")["allowance_requirement"]["spender"] == "0x" + "4" * 40
```

- [ ] **Step 2: Run tests and verify the expected missing-model failure**

Run: `uv run pytest tests/domain/test_models.py -q`

Expected: FAIL because the new model names and quote fields do not yet exist.

- [ ] **Step 3: Implement the minimal models and protocol**

Use `DomainModel`/`Asset` conventions, raw integer regex validation, and JSON-safe decimal/datetime fields. Keep optional fields defaulted so old provider fixtures still validate.

- [ ] **Step 4: Run focused and full domain tests**

Run: `uv run pytest tests/domain/test_models.py tests/domain/test_contracts.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wallet_agent/domain tests/domain/test_models.py
git commit -m "feat: add wallet action domain contracts"
```

### Task 2: Extend EVM adapter with balance, allowance, receipt, and calldata builders

**Files:**
- Modify: `src/wallet_agent/chains/evm.py`
- Modify: `src/wallet_agent/chains/_common.py`
- Modify: `src/wallet_agent/domain/providers.py`
- Test: `tests/chains/test_evm.py`

**Interfaces:**
- Implement `get_token_balance`, `get_allowance`, `get_transaction_receipt`, `build_native_transfer`, `build_erc20_transfer`, and `build_erc20_approve` exactly as specified in the design.
- Add `receipt_success`/ABI encoding helpers only if they remain deterministic and independently testable.

- [ ] **Step 1: Write failing adapter tests**

```python
def test_build_erc20_approve_encodes_standard_selector():
    tx = adapter.build_erc20_approve(
        token=usdc, owner=OWNER, spender=SPENDER, amount_raw="1000000"
    )
    assert tx.to == usdc.address
    assert tx.data.startswith("0x095ea7b3")
    assert tx.data.endswith(("0" * 64)[:-len("f" * 40)] + "f" * 40)

@pytest.mark.asyncio
async def test_allowance_reads_owner_and_spender_words():
    rpc = RecordingRpc({"eth_call": "0x" + ("0" * 63) + "f"})
    assert await EVMChainAdapter(rpc=rpc).get_allowance(usdc, OWNER, SPENDER) == "15"
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `uv run pytest tests/chains/test_evm.py -q`

Expected: FAIL with missing adapter methods or incorrect calldata.

- [ ] **Step 3: Implement deterministic encoding and reads**

Use selectors `a9059cbb` (`transfer(address,uint256)`), `095ea7b3` (`approve(address,uint256)`), and `dd62ed3e` (`allowance(address,address)`). Encode 32-byte address/uint words, reject malformed addresses and negative/non-integer raw amounts, and preserve `0x` prefixes. Native transfers use `to=recipient`, `data=0x`, `value=amount_raw`.

- [ ] **Step 4: Run chain tests and all existing chain tests**

Run: `uv run pytest tests/chains -q`

Expected: PASS with no network access.

- [ ] **Step 5: Commit**

```bash
git add src/wallet_agent/chains src/wallet_agent/domain/providers.py tests/chains/test_evm.py
git commit -m "feat: add EVM allowance and unsigned transaction builders"
```

### Task 3: Add CoinGecko price provider and configuration

**Files:**
- Create: `src/wallet_agent/prices/__init__.py`
- Create: `src/wallet_agent/prices/coingecko.py`
- Modify: `src/wallet_agent/config.py`
- Modify: `src/wallet_agent/main.py`
- Test: `tests/prices/test_coingecko.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Implement `CoinGeckoPriceProvider(transport, token_id_by_address, native_id_by_symbol, ttl_seconds)` with `get_prices`.
- Add `coingecko_api_key`, `coingecko_base_url`, `coingecko_token_ids`, `coingecko_native_ids`, and `price_cache_ttl_seconds` settings.
- Inject the provider into `GraphRuntime` and `create_app` without making price configuration mandatory for startup.

- [ ] **Step 1: Write failing normalization/cache tests**

```python
@pytest.mark.asyncio
async def test_coingecko_normalizes_prices_and_uses_ttl_cache():
    transport = FakeJsonTransport({"ethereum": {"usd": 3200.5}, "usd-coin": {"usd": 1.0}})
    provider = CoinGeckoPriceProvider(transport, token_id_by_address={USDC.lower(): "usd-coin"}, ttl_seconds=60)
    first = await provider.get_prices([eth_asset, usdc_asset])
    second = await provider.get_prices([eth_asset, usdc_asset])
    assert {item.asset.symbol for item in first} == {"ETH", "USDC"}
    assert transport.calls == 1
    assert second[0].observed_at is not None
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `uv run pytest tests/prices/test_coingecko.py -q`

Expected: FAIL because the package and provider do not exist.

- [ ] **Step 3: Implement normalized lookup and degraded errors**

Build one `simple/price` request per batch, send the optional API key header, cache only successful responses, and return an empty list with a typed retryable error path when CoinGecko is unavailable. Never include API keys in model state or logs.

- [ ] **Step 4: Run price/config tests**

Run: `uv run pytest tests/prices tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/wallet_agent/prices src/wallet_agent/config.py src/wallet_agent/main.py tests/prices tests/test_config.py
git commit -m "feat: add configurable CoinGecko token prices"
```

### Task 4: Extend provider quote metadata with allowance requirements and USD snapshots

**Files:**
- Modify: `src/wallet_agent/providers/bridgers.py`
- Modify: `src/wallet_agent/providers/omnibridge.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Test: `tests/providers/test_bridgers.py`
- Test: `tests/providers/test_omnibridge.py`

**Interfaces:**
- Provider quote normalization must preserve the explicit `provider_reference` and add an `allowance_requirement` when the response/config exposes an EVM spender/contract address.
- Graph quote response must retain every candidate in provider order-independent form and attach price snapshots without choosing a provider.

- [ ] **Step 1: Write failing provider tests**

```python
@pytest.mark.asyncio
async def test_bridgers_evm_quote_exposes_allowance_requirement():
    quote = await provider.quote(evm_swap_request)
    assert quote.allowance_requirement.spender == SWAP_CONTRACT
    assert quote.allowance_requirement.required_amount_raw == evm_swap_request.input_amount_raw
```

- [ ] **Step 2: Run provider tests and confirm failure**

Run: `uv run pytest tests/providers/test_bridgers.py tests/providers/test_omnibridge.py -q`

Expected: FAIL because quote models currently have no allowance metadata.

- [ ] **Step 3: Implement metadata extraction with safe absence behavior**

Read provider-specific spender/contract fields from normalized response metadata or explicit settings. If no spender exists, leave `allowance_requirement=None`; do not fabricate an approval target. Add price snapshots in the graph after quote fan-in, keyed by asset identity.

- [ ] **Step 4: Run provider and graph quote tests**

Run: `uv run pytest tests/providers tests/graph/test_graph_paths.py -q`

Expected: PASS and existing quote comparison behavior remains list-preserving.

- [ ] **Step 5: Commit**

```bash
git add src/wallet_agent/providers src/wallet_agent/graph/nodes.py tests/providers tests/graph/test_graph_paths.py
git commit -m "feat: expose quote authorization and price metadata"
```

### Task 5: Add transfer and swap-authorization graph nodes with explicit routing

**Files:**
- Modify: `src/wallet_agent/graph/state.py`
- Modify: `src/wallet_agent/graph/routes.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/graph/build.py`
- Test: `tests/graph/test_transfer_paths.py`
- Test: `tests/graph/test_swap_authorization.py`

**Interfaces:**
- New graph routes: `transfer`, `swap_select`, `swap_allowance`, `price_query`.
- Transfer node returns `response.kind="transfer_prepare"` with `pending_transaction` or a stable insufficient-balance error.
- Authorization node returns `response.stage` and `approval_transaction` or `pending_transaction`; it interrupts with `{"kind":"approval_required", "approval_transaction": {...}}` before any swap provider write.
- Resume path accepts only an approval transaction hash in state and rechecks receipt plus allowance before preparing the swap.

- [ ] **Step 1: Write failing path tests**

```python
@pytest.mark.asyncio
async def test_transfer_returns_unsigned_erc20_transaction_after_balance_check():
    result = await graph.ainvoke({"intent": "transfer", "request": transfer_payload()}, config=thread())
    assert result["response"]["kind"] == "transfer_prepare"
    assert result["pending_transaction"]["data"].startswith("0xa9059cbb")

@pytest.mark.asyncio
async def test_insufficient_allowance_interrupts_with_approve_transaction():
    result = await graph.ainvoke(selected_quote_state(), config=thread())
    assert result["response"]["stage"] == "approval_required"
    assert result["approval_transaction"]["data"].startswith("0x095ea7b3")

@pytest.mark.asyncio
async def test_successful_approval_resume_rechecks_allowance_before_prepare():
    await graph.ainvoke(selected_quote_state(), config=thread())
    result = await graph.ainvoke(Command(resume={"approve_tx_hash": APPROVE_HASH}), config=thread())
    assert result["response"]["stage"] == "swap_ready"
    assert fake_provider.prepare_calls == 1
```

- [ ] **Step 2: Run graph tests and verify failure**

Run: `uv run pytest tests/graph/test_transfer_paths.py tests/graph/test_swap_authorization.py -q`

Expected: FAIL because routes/nodes and state fields do not exist.

- [ ] **Step 3: Implement state fields and deterministic nodes**

Add reducers for quote candidates/errors, store selected `provider_reference`, validate ownership/expiry, read balances and gas, compare raw allowance, and place every provider prepare call after the allowance gate. Use a bounded approval resume path and stable stages; never auto-select the first quote.

- [ ] **Step 4: Run graph suite including persistence resume tests**

Run: `uv run pytest tests/graph -q`

Expected: PASS, including existing swap quote/confirm/status behavior.

- [ ] **Step 5: Commit**

```bash
git add src/wallet_agent/graph tests/graph
git commit -m "feat: add transfer and swap authorization graph paths"
```

### Task 6: Persist authorization fields and expose action endpoints

**Files:**
- Modify: `src/wallet_agent/persistence/store.py`
- Modify: `src/wallet_agent/api/app.py`
- Modify: `src/wallet_agent/main.py`
- Test: `tests/api/test_transfer_flow.py`
- Test: `tests/api/test_swap_authorization.py`
- Test: `tests/api/test_api_contract.py`

**Interfaces:**
- Add session projection fields for `stage`, `selected_provider_reference`, `approval_transaction`, `approval_tx_hash`, `allowance_requirement`, and `pending_transaction`.
- Add endpoints `POST /v1/transfer/{session_id}/prepare`, `POST /v1/swap/{session_id}/select-quote`, `POST /v1/swap/{session_id}/approve-broadcast`, `POST /v1/swap/{session_id}/continue`, and `GET /v1/prices/token`.
- `approve-broadcast` accepts only `{chain, approve_tx_hash}`; repeat same hash is idempotent and a different hash returns 409.

- [ ] **Step 1: Write failing API contract tests**

```python
@pytest.mark.asyncio
async def test_select_quote_requires_reference_and_returns_approval_action():
    response = await client.post(f"/v1/swap/{session_id}/select-quote", json={"provider_reference": REF})
    assert response.status_code == 200
    assert response.json()["stage"] == "approval_required"
    assert response.json()["approval_transaction"]["data"].startswith("0x095ea7b3")

@pytest.mark.asyncio
async def test_approve_hash_is_idempotent_and_conflicts_on_different_hash():
    first = await client.post(path, json={"chain": "BASE", "approve_tx_hash": APPROVE_HASH})
    second = await client.post(path, json={"chain": "BASE", "approve_tx_hash": APPROVE_HASH})
    conflict = await client.post(path, json={"chain": "BASE", "approve_tx_hash": "0x" + "b" * 64})
    assert first.status_code == second.status_code == 200
    assert conflict.status_code == 409
```

- [ ] **Step 2: Run API tests and verify failure**

Run: `uv run pytest tests/api/test_transfer_flow.py tests/api/test_swap_authorization.py -q`

Expected: FAIL because the endpoints and session fields do not exist.

- [ ] **Step 3: Implement request validation, ownership, and session projection**

Reuse `authenticated_user` and `owned_session`; validate EVM hashes with the existing chain-qualified helper; resume the durable graph with the session thread ID; return stable error envelopes and current stage for repeated requests.

- [ ] **Step 4: Run all API tests**

Run: `uv run pytest tests/api -q`

Expected: PASS with all legacy contracts unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/wallet_agent/api src/wallet_agent/persistence src/wallet_agent/main.py tests/api
git commit -m "feat: expose transfer and approval action APIs"
```

### Task 7: Update real mobile demo and integration documentation

**Files:**
- Modify: `demo/index.html`
- Modify: `docs/mobile-integration.md`
- Create: `docs/evm-wallet-actions.md`
- Test: `tests/api/test_demo_page.py`

**Interfaces:**
- Demo shows quote candidates and requires a selected `provider_reference`.
- Demo displays approval unsigned transaction, accepts an app-supplied approval hash, then calls continue and displays swap unsigned data.
- Demo never asks for or stores a private key/seed phrase.

- [ ] **Step 1: Write failing static contract tests**

```python
def test_demo_documents_quote_selection_and_approval_flow(client):
    html = client.get("/demo/").text
    assert "select-quote" in html
    assert "approve-broadcast" in html
    assert "provider_reference" in html
    assert "private key" in html.lower()
```

- [ ] **Step 2: Run test and verify failure**

Run: `uv run pytest tests/api/test_demo_page.py -q`

Expected: FAIL because the current demo only has generic confirm/broadcast controls.

- [ ] **Step 3: Implement UI state transitions and docs**

Add quote cards, selection, approval transaction display, hash submission, continuation, and ready-to-swap display. Document REST/SSE payloads and the mobile signer responsibilities with concrete JSON examples.

- [ ] **Step 4: Run demo/API tests**

Run: `uv run pytest tests/api/test_demo_page.py tests/api -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add demo docs tests/api/test_demo_page.py
git commit -m "docs: demonstrate EVM transfer and approval flow"
```

### Task 8: Full verification and deployment configuration

**Files:**
- Modify: `.env.example` (create if absent)
- Modify: `README.md`
- Modify: `docker-compose.yml`
- Test: `tests/integration/test_real_config.py` (integration-marked, skipped by default)

- [ ] **Step 1: Add configuration and smoke assertions**

Document CoinGecko settings, EVM token ID mapping, provider spender configuration, and the exact local Docker/demo commands. Add an opt-in integration test that checks configured endpoints only when `RUN_INTEGRATION_TESTS=1`.

- [ ] **Step 2: Run the complete verification suite**

Run: `uv run pytest -q && uv run ruff check . && uv run python -m compileall src tests && docker compose config --quiet`

Expected: all unit tests pass, integration tests are skipped unless explicitly enabled, Ruff and compileall pass, and Compose configuration validates.

- [ ] **Step 3: Review security invariants**

Run: `rg -n "private_key|seed_phrase|mnemonic|signer|wallet_client" src demo docs` and confirm every occurrence is a rejection rule or user-facing warning, never an accepted request field or log value.

- [ ] **Step 4: Commit verification artifacts**

```bash
git add .env.example README.md docker-compose.yml tests/integration
git commit -m "chore: document and verify EVM wallet action deployment"
```
