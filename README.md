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

## Docker 部署

Docker Compose 会启动 API、Next.js Demo、PostgreSQL 和 Redis。API 使用 `.env`
中的真实模型和 Provider 配置，不会自动使用 fake model/provider。首次部署：

```bash
cp .env.example .env
# 编辑 .env，至少设置 DEEPSEEK_API_KEY；按需配置 RPC、Bridgers、OmniBridge 和 OKX
docker compose up -d --build
```

之后可以使用仓库内的脚本重复部署（不会删除 PostgreSQL/Redis 数据卷）：

```bash
./scripts/redeploy.sh
./scripts/redeploy.sh --no-cache
./scripts/redeploy.sh --proxy http://host.docker.internal:7890
```

脚本会等待 API 的 `/health`、`/ready` 和 Demo 首页就绪；构建或健康检查失败时会输出最近的服务日志。

远程服务器部署时，Demo 在用户浏览器中运行，因此 `NEXT_PUBLIC_AGENT_API_URL`
必须填写浏览器可访问的 API 地址，不能填写 Compose 内部的 `wallet-agent:8000`：

```dotenv
NEXT_PUBLIC_AGENT_API_URL=http://<server-ip>:8000
CORS_ALLOW_ORIGINS=http://<server-ip>:3000
```

修改这两个值后需要重新构建 Demo；它们不是运行时热更新配置：

```bash
docker compose build --no-cache demo-next
docker compose up -d --force-recreate demo-next wallet-agent
```

如果修改了 Python 源码、依赖或 Dockerfile，强制重新构建并重建 API：

```bash
docker compose build --no-cache wallet-agent
docker compose up -d --force-recreate wallet-agent
```

如果当前环境需要 HTTP/HTTPS 代理，代理变量会被 Docker Compose 传递给构建命令：

```bash
HTTP_PROXY=http://127.0.0.1:7890 \
HTTPS_PROXY=http://127.0.0.1:7890 \
NO_PROXY=localhost,127.0.0.1,mysql,postgres,redis \
docker compose build wallet-agent demo-next
docker compose up -d
```

检查服务状态和日志：

```bash
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8000/ready
docker compose logs -f wallet-agent
```

成功启动后：

- API: [http://localhost:8000](http://localhost:8000)
- Demo: [http://localhost:3000](http://localhost:3000)

停止服务但保留 PostgreSQL/Redis 数据卷：

```bash
docker compose down
```

仅在确认要删除本地会话、checkpoint、SSE 事件和 Redis 数据时，才删除数据卷：

```bash
docker compose down -v
```

Docker 部署的 SSE 恢复和多实例配置见下面的
[Multi-instance persistence and SSE recovery](#multi-instance-persistence-and-sse-recovery)。

## Next.js Demo

Open [http://localhost:3000](http://localhost:3000) in a browser after Docker
starts. The Demo calls the API at `http://localhost:8000`, streams agent
progress and model/tool traces, and keeps wallet interaction in the browser.
The API no longer serves a legacy `/demo/` page.

Demo/API 调用示例见 [docs/demo-api-guide.md](docs/demo-api-guide.md)，App 端接入指南见
[docs/app-integration.md](docs/app-integration.md)，钱包集成说明见
[docs/mobile-integration.md](docs/mobile-integration.md)。

For a command-line check after startup:

```bash
python scripts/smoke_test.py --base-url http://localhost:8000
```

The default model backend is DeepSeek's official OpenAI-compatible API:
`https://api.deepseek.com` with `deepseek-flash`. To use another compatible
backend, set `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL` instead.
Classification, slot extraction, response generation, and read-only planning use
`deepseek-flash` by default. `deepseek-v4-pro` can be enabled as an explicit
fallback for complex reasoning. Read-only planning is bounded to four tool
rounds. Configure `READ_AGENT_ENABLED=false` to
keep the legacy deterministic read routes. The planner can only call balance, price, gas,
asset-discovery, market-detail, price-history, portfolio, and transaction-status tools; it cannot reach signing,
authorization, transfer, swap, or broadcast nodes.

The enabled read-only path uses DeepSeek native tool calls: Flash returns a
tool call, the graph executes the allowlisted read tool through `ToolNode`, and
the resulting `ToolMessage` is returned to Flash for the next decision. Write
capabilities remain deterministic graph nodes behind wallet confirmation and
are never exposed as model tools.

The HTTP API is rooted at `/v1`: agent turns and SSE streaming, explicit swap
quote selection (`/swap/{session_id}/select-quote`), expiring confirmation state,
allowance/approval gating
(`/approve-broadcast` then `/continue`), transfer preparation and broadcast-hash
registration (`/transfer/{session_id}/broadcast`), token prices, and read-only
wallet balances, transactions, fee estimates, and transaction status lookup
(`/transactions/{chain}/{tx_hash}`). Transfer and swap preparation return
machine-readable preflight checks before signing. The app must choose the returned
`provider_reference`; the service never picks a quote silently.
Use a stable `conversation_id` as the LangGraph `thread_id` when reconnecting.

## Multi-instance persistence and SSE recovery

Local development keeps the zero-service defaults: SQLite stores LangGraph
checkpoints/session projections and run events/locks remain in process. For two
or more API instances, configure both shared backends:

```dotenv
PERSISTENCE_URL=mysql://wallet_agent:change-me@mysql:3306/wallet_agent
REDIS_URL=redis://redis:6379/0
REDIS_KEY_PREFIX=wallet-agent
RUN_EVENT_TTL_SECONDS=86400
RUN_EVENT_MAX_ENTRIES=2000
CONVERSATION_LOCK_LEASE_SECONDS=120
CONVERSATION_LOCK_WAIT_SECONDS=30
```

MySQL owns LangGraph checkpoints and the app-facing session projection in both
the Compose stack and production: `PERSISTENCE_URL=mysql://user:pass@host:3306/db`
(the driver is rewritten to `mysql+asyncmy`). Checkpoints use the community
`langgraph-checkpoint-mysql` package on its asyncmy backend. It deliberately avoids
aiomysql: PyMySQL >= 1.2 replaced `escape_bytes_prefixed` with a string sentinel, which
breaks aiomysql's binary parameter escaping and silently loses checkpoint blobs.
`cryptography` must stay installed for MySQL 8's default `caching_sha2_password` auth
-- without it the failure surfaces as a misleading "Lost connection to MySQL server
during query".
Keep the server's stock utf8mb4 collation: `langgraph-checkpoint-mysql` compares
`checkpoint_ns_hash` against `UNHEX(MD5(...))`, and creating the tables under a
different `collation_server` (for example `utf8mb4_unicode_ci`) makes every
checkpoint read fail with `(1267, "Illegal mix of collations ...")`. Verify the
target server's default collation before pointing production at it.
Redis Streams owns the cross-instance run-event log, while a renewable Redis
lease serializes turns for the same `conversation_id`. Every SSE frame includes
an `id`. Reconnect to the same `run_id` with `Last-Event-ID` or
`?last_event_id=...`; only later events are replayed. Resume is bounded by
`RUN_EVENT_TTL_SECONDS` and stream trimming.

`docker compose up --build` starts the API with MySQL and Redis. The bundled
MySQL service sits behind the `bundled-db` profile, enabled by
`COMPOSE_PROFILES=bundled-db` in `.env` (see `.env.example`); a deployment that
sets `DEPLOY_PERSISTENCE_URL` to its own server leaves the profile off and starts
no local database. The Compose credentials are local-development defaults;
replace them in a real deployment. Postgres is still defined behind the
`postgres` profile (`docker compose --profile postgres up -d`) for anyone
testing that backend. An interrupted process is not silently re-executed by
another API replica: persisted checkpoints make an explicit retry/resume safe,
while Redis preserves events already emitted to the client.

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
reports. `/v1/swap/{session_id}/broadcast` and
`/v1/transfer/{session_id}/broadcast` accept only a hash and chain.
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

`pytest -q` runs offline on in-memory and SQLite savers. The Postgres/MySQL savers and
session store are covered by integration tests that skip unless a live database is
provided:

```bash
TEST_POSTGRES_URL=postgresql://user:pass@127.0.0.1:5432/postgres \
TEST_MYSQL_URL=mysql://user:pass@host:3306/wallet_agent \
  .venv/bin/pytest -q -m integration tests/persistence/test_savers_integration.py
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
