# Bridgers API — Endpoint Reference

> Source: https://docs-bridgers.bridgers.xyz/bridgers-api-jie-kou/ (6 pages)
> Distilled: 2026-08-03. Base URL: `https://api.bridgers.xyz`
> Success is `resCode == "100"` (string) or `100` (number) on every endpoint.
> Error/status codes: see `status-and-error-codes.md`.

## Cross-cutting rules
- `equipmentNo` = unique user identifier. Codebase uses first 32 chars of `fromAddress`.
- `sourceFlag` = channel identifier agreed with Bridgers (`BRIDGERS_SOURCE_FLAG` env). If unregistered, requests fail at runtime.
- `fromCoinCode` / `toCoinCode` = `SYMBOL(CHAIN)` string, e.g. `USDC(BASE)`. **Not returned by any endpoint — must be built client-side.**
- Amounts in request bodies are **raw integer wei strings** (already scaled by decimals).
- `slippage` is a decimal-fraction string: `"0.1"` means 10% (NOT 0.1%).

---

## 1. Request Quote — `POST /api/sswap/quote`
Client: `bridgers::get_quote`.

**Request** (required*): `equipmentNo*`, `sourceFlag*`, `fromTokenAddress*`, `toTokenAddress*`,
`fromTokenAmount*` (raw wei), `fromTokenChain*`, `toTokenChain*`. Optional: `userAddr`, `sourceType`, `fixedRateFlag`, `fromCoinCode`, `toCoinCode`.

**Response `data.txData`**:
| field | meaning | unit |
|---|---|---|
| `toTokenAmount` | expected output | **human-readable decimal** |
| `amountOutMin` | min acceptable output | **raw wei** |
| `fromTokenAmount` | echo of input | raw wei |
| `fromTokenDecimal` | input token decimals | integer |
| `toTokenDecimal` | output token decimals | integer |
| `depositMin` / `depositMax` | swap bounds | **human decimal, fromToken units** |
| `fee` | swap fee rate | proportion (`0.002` = 0.2%) |
| `chainFee` | on-chain miner fee | decimal, native currency |
| `contractAddress` | swap contract (approve target) | address |
| `dex`, `feeToken`, `path[]`, `logoUrl` | routing metadata | — |

**No `quoteId`, no TTL/expiry field.** Quote and swap are separate requests (re-quoted at build time).

---

## 2. Obtain CallData — `POST /api/sswap/swap`
Client: `bridgers::build_swap_tx`.

**Request** (required*): all of quote's fields plus `fromAddress*`, `toAddress*`,
`amountOutMin*` (raw wei, from quote), `fromCoinCode*`, `toCoinCode*`. Optional: `sourceType`, `slippage`, `fixedRateFlag`.

**Response `data.txData` (EVM/TRON)** — **only three fields**:
| field | meaning | format |
|---|---|---|
| `data` | transaction calldata | `0x…` hex |
| `to` | target contract | address |
| `value` | native value to send | **`0x…` hex wei** |

> ⚠️ **No `toTokenAmount`, no `gasLimit`, no receive amount here.** The output amount
> for display MUST come from the quote (`toTokenAmount` / `amountOutMin`), not this response.
> Gas MUST be obtained via `eth_estimateGas` on `{to, data, value}` — the API never returns it.
> (Solana returns `tx` + optional `signer` instead; not used by this codebase.)

Formula the API applies: `minReceived = amountOutMin × (1 - slippage)`.

---

## 3. Get Coins List — `POST /api/exchangeRecord/getToken`
Client: `bridgers::get_tokens`.

**Request**: `chain` (String, optional). Chain values: `ETH, BSC, HECO, POLYGON, OKEXCHAIN,
TRX, FTM, ARB, AVAXC, Optimism, CRONOS, APT, ZKSYNC, MNT, CORE, SUI, CFX, BASE, opBNB, zkEVM, SCROLL, SOL, LINEA, Blast`.

**Response `data.tokens[]`** per token: `chain`, `symbol`, `name`, `address`, `decimals` (Number),
`logoURI`, `isCrossEnable`, `withdrawGas`.

> Provides authoritative `decimals` + `address` per chain → use to resolve decimals for
> address inputs instead of a hardcoded symbol table. **No `coinCode` field** — build `SYMBOL(CHAIN)` yourself.

---

## 4. Generate Order Info — `POST /api/exchangeRecord/updateDataAndStatus`
Client: `bridgers::upload_order_hash`. Called **after** broadcasting the on-chain tx.

**Request** (required*): `equipmentNo*`, `sourceFlag*`, `hash*` (broadcast tx hash),
`fromTokenAddress*`, `toTokenAddress*`, `fromAddress*`, `toAddress*`, `fromTokenChain*`, `toTokenChain*`,
`fromTokenAmount*` (raw wei), `amountOutMin*` (raw wei), `fromCoinCode*`, `toCoinCode*`. Optional: `sourceType`, `slippage`.

**Response**: `data.orderId` (String) — **the real order id. Capture and use this for polling.**

> ⚠️ **Order is created here, after broadcast** — if this call fails, the trade is on-chain
> but Bridgers has no record. Must NOT be fire-and-forget.
> **Idempotency**: re-submitting the same `hash` returns `resCode 414` ("already updated") — so
> retry is SAFE; treat 414 as success, not an error.

---

## 5. Query Transaction Records — `POST /api/exchangeRecord/getTransData`
Client: `bridgers::query_records`. Paginated history.

**Request** (required*): `equipmentNo*`, `sourceFlag*`, `pageNo*`, `pageSize*`, `fromAddress*`. Optional: `sourceType`.
Ownership is scoped by `equipmentNo` + `fromAddress` **at Bridgers** — the caller must still
enforce that the requester owns `fromAddress` (see COW-96 §6: `/swap/history` ownership gap).

**Response**: `data.list[]` (records), `data.total`, `data.pageNo`. Record fields match §6 detail fields.

---

## 6. Query Transaction Details — `POST /api/exchangeRecord/getTransDataById`
Client: `bridgers::get_order_status`.

**Request**: `orderId*` (String) — **ONLY the real orderId from §4. Does NOT accept a txHash.**
(Passing a txHash will never match — this is the COW-96 §4 mobile bug.)

**Response `data`**: `id`, `orderId`, `status` (see `status-and-error-codes.md`), `fromTokenAddress`,
`toTokenAddress`, `fromTokenAmount`, `toTokenAmount`, `fromAddress`, `toAddress`, `slippage`,
`fromChain`, `toChain`, `hash` (deposit), `toHash` (send), `depositHashExplore`, `receiveHashExplore`,
`dexName`, `createTime`, `source`, `equipmentNo`, `fromCoinCode`, `toCoinCode`,
`refundCoinAmt`, `refundHash`, `refundHashExplore`, `refundReason`.

> Refunds ARE documented (contra COW-96 vendor table): see the `refund*` fields + refund statuses.
