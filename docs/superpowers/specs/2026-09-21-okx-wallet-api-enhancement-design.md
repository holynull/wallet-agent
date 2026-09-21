# OKX Wallet API Non-Custodial Enhancement Design

## Goal

Integrate the OKX Onchain OS Wallet and Market APIs as an optional enhancement layer for
the existing wallet agent while preserving the browser wallet as the only
signer. Existing balance, portfolio, gas, preflight, and transaction-status
flows gain OKX-backed data or validation; Wallet and price capabilities that
do not yet exist in the agent are exposed through normalized domain contracts
and REST/agent entry points.

This design explicitly excludes OKX DEX aggregation and cross-chain swap APIs.
OmniBridge and Bridgers remain the only swap providers.

## Non-custodial boundary

- The browser extension or mobile wallet remains the only holder of private
  keys and the only component allowed to request a user signature.
- The backend never accepts or stores a private key, seed phrase, signer,
  wallet client, signed transaction, or raw transaction through a Wallet API
  request.
- OKX API credentials are server-side deployment secrets. They are never
  returned to the Demo, model, graph state, SSE stream, logs, or error detail.
- The backend may return unsigned transaction data and preflight results. The
  user must still approve the transaction in the wallet.
- The browser wallet continues to call `eth_sendTransaction`. OKX APIs can
  validate and observe the transaction, but cannot guarantee propagation when
  the injected wallet uses a different RPC.

## Confirmed OKX protocol contract

The endpoint and authentication details in this design come from the OKX
Onchain OS documentation indexed by Context7 under
`/websites/web3_okx_zh-hans_onchainos_dev-docs`.

Authenticated requests use:

- `OK-ACCESS-KEY`
- `OK-ACCESS-SIGN`
- `OK-ACCESS-TIMESTAMP`
- `OK-ACCESS-PASSPHRASE`
- `OK-ACCESS-PROJECT` when required by the selected OKX project

The signature is Base64-encoded HMAC-SHA256 using the API secret. The prehash
is:

```text
timestamp + uppercase HTTP method + request path (including GET query) + body
```

The timestamp is ISO-8601 UTC. POST bodies are compact JSON and the exact bytes
used to calculate the signature are sent on the wire. GET query parameters are
encoded once in deterministic order and that same query string is used for the
signature and request.

OKX responses use a JSON envelope with string `code`, `msg`, and `data`.
`code == "0"` is success. HTTP success with a nonzero OKX code is an
application error, not a successful empty result.

## In-scope APIs

The first implementation uses only endpoints confirmed in the Wallet and
pre-transaction documentation. It does not call similarly named deprecated
`/api/v5` examples when a documented `/api/v6` endpoint is available.

### Wallet balance APIs

1. `GET /api/v6/dex/balance/total-value-by-address`
   - Inputs: address, comma-separated chain indexes, asset type, and risk-token
     exclusion.
   - Adds multi-chain total USD value, including optional DeFi value when
     requested.
2. `GET /api/v6/dex/balance/all-token-balances-by-address`
   - Inputs: address, comma-separated chain indexes, and risk-token exclusion.
   - Returns normalized token balances with raw balance where available, price,
     contract, chain, symbol, and risk-token flag.

These endpoints enhance the current portfolio query. Existing direct RPC
balances remain available as a fallback and as the source of truth for signing
prechecks when OKX is unavailable or stale.

### Pre-transaction APIs

1. `POST /api/v6/dex/pre-transaction/gas-limit`
   - Inputs: chain index, from address, to address, native amount, and calldata.
   - Enhances the existing `eth_estimateGas` result.
2. `POST /api/v6/dex/pre-transaction/simulate`
   - Inputs: the same transaction context.
   - Adds simulation success, gas used, and a sanitized failure reason to the
     existing preflight response.

Simulation is advisory and cannot authorize a transaction. A successful OKX
simulation does not bypass existing address, account, chain, balance,
allowance, confirmation, or wallet-signature checks.

### Market price APIs

Price lookup is in scope even though the endpoints live under the OKX Market
API namespace. These are read-only market-data APIs, not OKX DEX aggregation or
swap execution.

1. `POST /api/v6/dex/market/price`
   - Batch lookup of the current USD price and observation time by chain index
     and token contract address.
   - Enhances the existing `TokenPriceProvider` and `/v1/prices/token` flow.
2. `POST /api/v6/dex/market/price-info`
   - Batch lookup of detailed market metrics: current price, market cap,
     short-period price changes, volumes, transaction counts, 24-hour
     high/low, circulating supply, liquidity, and holder count.
   - Adds a detailed market response without mixing those fields into the
     minimal quote price snapshot.
3. `GET /api/v6/dex/index/historical-price`
   - Returns USD index-price points with `1m`, `5m`, `30m`, `1h`, or `1d`
     periods, millisecond time bounds, cursor pagination, and a maximum of 200
     records per request.
   - Supports a blank contract address for the chain native asset where the
     OKX documentation permits it.
4. `GET /api/v6/dex/market/candles`
   - Returns OHLC, token volume, USD volume, and candle completion state.
   - Supports documented bar sizes and timestamp cursors, with at most 299
     rows per page and 1,440 candles per granularity.

The specialized meme-token WebSocket channel is not part of this delivery. It
is a streaming discovery feed rather than the address-driven price-query
capability requested here, and it does not provide a generic replacement for
the four HTTP endpoints above.

### Existing non-OKX capabilities

The following stay in their existing implementations because the selected
Wallet API scope does not provide a confirmed replacement:

- transaction signing and `eth_sendTransaction`;
- receipt and mempool lookup through configured chain RPCs;
- ERC-20 allowance reads and deterministic transfer/approve calldata;
- OmniBridge and Bridgers quote/order flows;
- CoinGecko price fallback where OKX does not return a usable price.

No undocumented OKX endpoint is guessed. Additional Wallet API endpoints can
be added to the same client after their exact v6 path and schema are present in
the official documentation, without changing graph or API contracts.

## Architecture

```text
Agent / REST API
       |
       v
Normalized wallet capability services
  |             |                    |
  v             v                    v
OKX Wallet   Existing chain RPC   Existing providers
+ Market     adapters             (CoinGecko,
adapters                          OmniBridge, Bridgers)
  |
  v
Signed OKX HTTP client

Browser wallet
  -> user confirmation
  -> local signing
  -> eth_sendTransaction
  -> backend broadcast registration and multi-source observation
```

The graph and API consume normalized models, never raw OKX response objects.
This prevents OKX field names and version changes from leaking throughout the
application.

## Components

### Configuration

Add optional settings:

```text
OKX_ENABLED
OKX_BASE_URL=https://web3.okx.com
OKX_API_KEY
OKX_SECRET_KEY
OKX_PASSPHRASE
OKX_PROJECT_ID
OKX_MAX_ATTEMPTS
OKX_CACHE_TTL_SECONDS
OKX_EXCLUDE_RISK_TOKENS
```

When `OKX_ENABLED=false`, no OKX credential is required and existing behavior
is unchanged. When enabled, all four credential values are validated at
startup. Secret values use Pydantic secret types and are redacted from model
representations.

### Signed transport

Create a focused OKX transport responsible for canonical query encoding,
compact body serialization, signing, headers, bounded retries, response
envelope validation, and secret-safe errors.

Retry only transport failures, HTTP 429, and HTTP 5xx. Do not retry
authentication errors, validation errors, or other nonzero provider codes.
Respect `Retry-After` when present and use bounded exponential backoff with
jitter. Logs contain method, path, status, OKX code, attempt count, and elapsed
time, but never headers, bodies, addresses, hashes, credentials, or raw error
payloads.

### OKX wallet adapter

Create an adapter with narrow methods:

```python
async def get_total_value(
    address: str,
    chain_indexes: list[str],
    *,
    asset_type: str = "0",
    exclude_risk_tokens: bool = True,
) -> WalletTotalValue

async def get_token_balances(
    address: str,
    chain_indexes: list[str],
    *,
    exclude_risk_tokens: bool = True,
) -> list[TokenBalance]

async def estimate_gas_limit(transaction: TransactionContext) -> GasLimitEstimate

async def simulate_transaction(transaction: TransactionContext) -> SimulationResult
```

An explicit chain-index mapper converts canonical agent chain names to OKX
indexes. Unknown chains return `capability_unavailable`; they are never sent as
guessed identifiers.

### OKX price adapter

Extend the current price-provider abstraction without exposing OKX response
shapes:

```python
async def get_prices(assets: list[Asset]) -> list[TokenPrice]

async def get_market_details(
    assets: list[Asset],
) -> list[TokenMarketDetails]

async def get_historical_prices(
    asset: Asset,
    *,
    period: PricePeriod,
    begin_ms: int | None = None,
    end_ms: int | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> HistoricalPricePage

async def get_candles(
    asset: Asset,
    *,
    bar: CandleBar,
    before_ms: int | None = None,
    after_ms: int | None = None,
    limit: int = 100,
) -> list[PriceCandle]
```

Native assets and contract assets use explicit chain-index/address mapping.
The adapter lowercases EVM contract addresses for candle requests as required
by OKX. It validates enums and provider limits before the HTTP call and parses
all monetary values as `Decimal`; timestamps become timezone-aware UTC values.

`TokenPrice` gains an explicit `provider` value so OKX and CoinGecko results
remain distinguishable. Detailed market metrics, historical points, and
candles use separate models instead of overloading the minimal price snapshot.

Current prices use a short configurable TTL and request coalescing. Historical
and candle pages use cache keys containing the complete normalized query. An
empty provider result is “price unavailable,” not a zero price.

### Price provider selection

When OKX is enabled, it is the primary current-price source for assets that can
be mapped to an OKX chain index and contract address. CoinGecko remains the
fallback for unsupported assets, missing OKX results, or an OKX outage.

Batch results merge per asset rather than treating the whole batch as success
or failure. Quote valuation and portfolio valuation may therefore contain OKX
and CoinGecko snapshots in one response, with the provider recorded on every
snapshot. Prices are never averaged across providers. The freshest successful
primary result wins; fallback fills only missing assets.

### Portfolio aggregation

The current portfolio service invokes OKX and existing chain providers
concurrently when OKX is enabled. Results are merged by canonical
`chain + contract address`, falling back to `chain + symbol` only for native
assets. The response retains source and observation time.

Direct chain RPC raw balances win conflicts because they are used for spending
checks. OKX contributes USD price, risk classification, broader token
discovery, and multi-chain totals. A provider failure produces a structured
provider error but does not erase successful results from another source.

### Gas and simulation

Preflight calls the existing RPC estimator and OKX gas-limit/simulation APIs
concurrently. It returns both observations and a conservative wallet gas limit:

```text
max(existing RPC gas estimate, OKX gas limit, OKX simulated gas used)
```

The EIP-1559 fee fields remain chain-RPC-derived because the confirmed OKX
pre-transaction endpoints provide gas usage, not a general priority-fee API.
The estimator must stop accepting a zero `eth_maxPriorityFeePerGas` without
validation. It uses `eth_feeHistory` where supported and otherwise a
configuration-bounded positive fallback, while ensuring:

```text
maxFeePerGas >= latest baseFeePerGas + maxPriorityFeePerGas
```

The response records each value's source. This addresses the previously
observed zero-priority-fee transaction without falsely attributing the fix to
an OKX endpoint that does not provide that field.

### Broadcast observation

Registration of a wallet-returned hash immediately queries transaction and
receipt visibility. The normalized states are:

- `broadcast_seen`: transaction or receipt is visible;
- `broadcast_pending`: visible but unmined;
- `not_propagated`: still absent after the short propagation window;
- `confirmed`: receipt succeeded;
- `failed`: receipt failed;
- `dropped_or_replaced`: previously visible transaction later disappears or
  the sender nonce advances without this hash confirming;
- `unknown`: all observers failed.

Provider order polling starts only after the source transaction is visible.
`not_propagated` must never be presented as “Provider is processing.”

## Public API and graph behavior

Existing endpoints remain backward compatible:

- `GET /v1/wallet/{address}/portfolio` gains OKX-derived assets, total value,
  risk fields, and per-source errors.
- `GET /v1/prices/token` retains its current contract and uses the composite
  OKX-primary/CoinGecko-fallback provider.
- Transaction preflight responses gain `gas_sources` and optional
  `simulation` fields.
- Broadcast responses and status turns use the expanded status taxonomy.

Add a read-only endpoint for clients that need the OKX-enhanced total without
starting an agent turn:

```text
GET /v1/wallet/{address}/total-value?chains=ETH,BSC&asset_type=all
GET /v1/prices/market?chain=ETH&address=0x...
GET /v1/prices/history?chain=ETH&address=0x...&period=1h&limit=50
GET /v1/prices/candles?chain=ETH&address=0x...&bar=1H&limit=100
```

The existing `price_query` intent continues to answer a simple current-price
question. It also recognizes explicit market-detail, history, and candle
requests and routes them to the corresponding narrow service. Cursor and limit
validation happens outside the model; the model cannot construct arbitrary
OKX paths or parameters.

No public endpoint accepts OKX credentials. Natural-language intents continue
to route through the existing portfolio, gas, transfer, swap, and status
flows; the supervisor does not receive a generic arbitrary OKX tool.

## Demo behavior

The Demo continues to use the injected EIP-1193 provider. It displays:

- OKX as a data or simulation source when used;
- OKX or CoinGecko as the source of each displayed price;
- current price, observation time, and optional market metrics;
- historical-price/candle results as structured data suitable for a client
  chart, while the simple Demo may render a compact table;
- risk-token filtering and unavailable-source warnings;
- separate gas-limit and EIP-1559 fee sources;
- `not_propagated` as a wallet/RPC broadcast problem;
- provider order progress only after source-chain visibility.

The backend debug panel includes the normalized OKX result and error code, not
credentials or signed headers.

## Errors and degradation

Use stable internal codes for invalid OKX configuration, authentication,
rate-limit, unsupported chain, invalid provider response, unavailable
capability, and simulation failure. Provider messages are sanitized before
being returned to clients.

OKX is an enhancement dependency:

- balance and total-value failures fall back to existing providers;
- gas-limit failure falls back to RPC estimation;
- simulation unavailability is a warning, not an automatic rejection;
- a successful simulation with an explicit execution failure blocks signing;
- current-price lookup falls back per asset to CoinGecko;
- detailed market, history, and candle failures return `unavailable` with an
  OKX source error because CoinGecko does not currently implement equivalent
  normalized endpoints;
- authentication/configuration failures are surfaced to operators and not
  retried indefinitely.

## Testing

All unit and API tests use deterministic `httpx.MockTransport` or existing RPC
fakes. Live OKX calls are integration-marked and skipped by default.

Coverage includes:

1. byte-for-byte signing for GET queries and POST bodies;
2. credential and log redaction;
3. success envelope normalization and nonzero-code errors;
4. retry boundaries for 429/5xx versus authentication/validation failures;
5. balance normalization, risk-token filtering, chain mapping, and pagination
   or provider limits where documented;
6. portfolio merge precedence and partial-provider failure;
7. gas-limit and simulation request/response normalization;
8. current-price batching, mixed OKX/CoinGecko fallback, caching, timestamps,
   and provider attribution;
9. detailed market metrics, historical cursor pages, candle tuple parsing,
   enum/limit validation, native assets, and lowercase EVM addresses;
10. zero-priority-fee fallback and EIP-1559 invariants;
11. `not_propagated`, confirmed, failed, dropped/replaced, and observer-failure
   status classification;
12. Demo rendering and credential non-disclosure;
13. OKX-disabled backward compatibility.

## Delivery sequence

1. Signed OKX transport, validated configuration, chain-index mapping, and
   deterministic contract tests.
2. Wallet balance and total-value adapter integrated with portfolio responses.
3. Current/detailed price, history, and candle adapters integrated with the
   existing price query and portfolio/quote valuation flows.
4. Pre-transaction gas-limit/simulation integration plus robust EIP-1559 fee
   calculation.
5. Broadcast visibility taxonomy and Provider-order gating.
6. Demo source/status/price presentation, debug redaction, integration tests, and
   operator documentation.

There is deliberately no OKX DEX or cross-chain provider phase.
