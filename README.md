# Wallet LangGraph Agent

Non-custodial Python/LangGraph service for a mobile crypto wallet. The app
keeps private keys and signers locally, displays unsigned transactions, signs
and broadcasts them, then submits only the chain-qualified transaction hash.

## Run locally

```bash
cp .env.example .env
# Set DEEPSEEK_API_KEY and explicitly enable providers with their base URLs.
uv sync
./scripts/start_local.sh
```

开发过程中需要热重载时，可以运行 `./scripts/start_local.sh --reload`。

## Docker + real mobile-call demo

The Docker service uses the real configuration from `.env`; it does not use a
fake model or fake provider. Set `DEEPSEEK_API_KEY`, configure the RPC URLs, and
enable Bridgers/OmniBridge only when their source flags and sandbox/production
access are ready.

```bash
cp .env.example .env
# edit .env and set the real key and provider settings
docker compose up --build
```

Open [http://localhost:8000/demo/](http://localhost:8000/demo/) in a browser.
The page calls the same REST/SSE endpoints that a mobile app uses: turn,
stream, quote selection, approval, session, and broadcast. It never accepts or
sends private keys. After the user explicitly selects a quote, the browser
wallet signs and broadcasts each unsigned transaction locally; the demo sends
only the returned chain-qualified transaction hash to the service.

本地调试的完整步骤（包括浏览器 Network/Console、SSE、报价选择和
approve/兑换交易流程）见 [docs/local-demo-debugging.md](docs/local-demo-debugging.md)。

Demo 现在支持对话式兑换和 EIP-1193 浏览器钱包连接；钱包插件只提供公开地址和链信息，签名仍在浏览器钱包本地完成。详见 [docs/mobile-integration.md](docs/mobile-integration.md)。

For a command-line check after startup:

```bash
python scripts/smoke_test.py --base-url http://localhost:8000
```

The default model backend is DeepSeek's official OpenAI-compatible API:
`https://api.deepseek.com` with `deepseek-v4-pro`. To use another compatible
backend, set `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL` instead.

The HTTP API is rooted at `/v1`: agent turns and SSE streaming, explicit swap
quote selection (`/swap/{session_id}/select-quote`), expiring confirmation state,
allowance/approval gating
(`/approve-broadcast` then `/continue`), transfer preparation, token prices, and
read-only wallet balances, transactions, fee estimates, and transaction status
lookup (`/transactions/{chain}/{tx_hash}`). Transfer and swap preparation return
machine-readable preflight checks before signing. The app must choose the
returned `provider_reference`; the service never picks a quote silently.
Use a stable `conversation_id` as the LangGraph `thread_id` when reconnecting.

能力扩展路线见 [docs/wallet-agent-capability-roadmap.md](docs/wallet-agent-capability-roadmap.md)。
资产组合和 Gas 助手还提供
`/wallet/{address}/portfolio`、`/wallet/{address}/gas`，也可通过对话
intent `portfolio_query`、`gas_check` 调用。Token 资产发现提供
`GET /assets?chain=BASE&search=USDC` 和对话 intent `asset_discovery`。

## Optional OKX Wallet and Market enhancement

Set `OKX_ENABLED=true` plus `OKX_API_KEY`, `OKX_SECRET_KEY`,
`OKX_PASSPHRASE`, and `OKX_PROJECT_ID` to enable the signed OKX Onchain OS
read-only integration. OKX is the primary wallet/market data source and supplies
portfolio totals/token balances, transaction history/detail/status, current and
detailed prices, history/candles, gas-limit estimates, and transaction simulation.
If an OKX price is unavailable, USD
enrichment is omitted without blocking the underlying swap quote. Direct chain
RPC is isolated to execution-critical allowance, receipt, nonce, fee, approval,
and browser-broadcast visibility checks.

The integration uses only these documented `/api/v6` endpoints:

- `/api/v6/dex/balance/total-value-by-address`
- `/api/v6/dex/balance/all-token-balances-by-address`
- `/api/v6/dex/balance/token-balances-by-address`
- `/api/v6/dex/post-transaction/transactions-by-address`
- `/api/v6/dex/post-transaction/transaction-detail-by-txhash`
- `/api/v6/dex/market/price`
- `/api/v6/dex/market/price-info`
- `/api/v6/dex/index/historical-price`
- `/api/v6/dex/market/candles`
- `/api/v6/dex/pre-transaction/gas-limit`
- `/api/v6/dex/pre-transaction/simulate`

OKX DEX aggregation and cross-chain swap APIs are intentionally not connected;
Bridgers and OmniBridge remain the swap providers. The browser/mobile wallet is
still the only signer and broadcaster. No private key, seed phrase, signer,
signed transaction, OKX credential, or signed header enters REST/SSE, graph
state, logs, or Demo debug output.

Additional normalized routes include `/v1/wallet/{address}/total-value`,
`/v1/prices/market`, `/v1/prices/history`, and `/v1/prices/candles`.

For swap authorization, the service returns an ERC-20 `approval_transaction`
when allowance is insufficient. Sign and broadcast that transaction in the app,
submit only its hash, then call `/continue`. The service verifies the receipt
and rereads allowance before calling the provider to create the final unsigned
swap transaction. See [docs/evm-wallet-actions.md](docs/evm-wallet-actions.md)
for request/response examples.

Provider and chain transports are injectable, so tests never call external
services. Unsupported chain families return `CHAIN_CAPABILITY_UNAVAILABLE`;
they are not presented as partially implemented capabilities.

## Security boundary

Do not put private keys, seed phrases, signer objects, wallet clients, or
provider credentials in requests, prompts, checkpoints, logs, or issue
reports. `/v1/swap/{session_id}/broadcast` accepts only a hash and chain.
The backend distinguishes `not_propagated` from `broadcast_pending`: a hash
that is still absent from source-chain RPC is saved for retry, but Provider
registration/polling does not start until the transaction is visible.

## Verification

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests
python3 -m compileall src
.venv/bin/python -m evals.wallet_agent_evals
.venv/bin/python -m evals.wallet_app_evals
.venv/bin/python -m evals.wallet_app_evals --scenario approval_pending_restart_resume
.venv/bin/pytest -q tests/browser -m browser
```

`wallet_app_evals` drives the real FastAPI REST/SSE interface and compiled
LangGraph lifecycle with deterministic wallet, provider, and chain simulators.
It runs fully offline: it does not load production credentials, sign a
transaction, contact a real Provider/RPC endpoint, or broadcast on-chain.

Core wallet capability evals use fake chain/provider backends, so they never
sign or broadcast transactions:

```bash
.venv/bin/python -m evals.wallet_agent_evals
.venv/bin/python -m evals.wallet_agent_evals --online
```

The first command runs the deterministic Phase 1 15-case CI suite. The opt-in
online mode invokes the configured language model for Chinese intent extraction,
slot normalization, corrections, cancellation, and multi-turn retention, while
wallet backends remain simulated. See [evals/README.md](evals/README.md) for
the case coverage and safety boundary.
