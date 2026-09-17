# Mobile integration checklist

The wallet app remains non-custodial: private keys, seed phrases, and signers
never leave the device. The service returns quotes, price context, and unsigned
EVM transactions only. The app presents them to the user, signs locally,
broadcasts through its wallet stack, and submits only a chain-qualified hash.

## Swap with explicit quote selection and approval

### Browser wallet variant

For the browser demo, obtain only public wallet context through the EIP-1193
provider. CatWallet is supported when its browser extension exposes that
standard provider. The demo also listens for EIP-6963 provider announcements,
checks `window.ethereum.providers`, and prioritizes CatWallet when several
browser wallets are installed:

```javascript
const accounts = await window.ethereum.request({method: "eth_requestAccounts"});
const chainId = await window.ethereum.request({method: "eth_chainId"});
```

Send the address and network as `address`, `chain`, and optional
`metadata.wallet_chain_id` on every conversational turn. Reuse the returned
`conversation_id` and `session_id` while collecting missing swap fields. Never
send `window.ethereum`, a signer, wallet client, private key, or seed phrase to
the service.

The service returns unsigned transactions. The browser wallet signs locally:

```javascript
const txHash = await window.ethereum.request({
  method: "eth_sendTransaction",
  params: [{from: address, to: tx.to, data: tx.data, value: tx.value || "0x0"}]
});
```

Submit only `txHash` to the existing approve and broadcast endpoints. Check the
transaction `chain_id` against the active wallet network before calling
`eth_sendTransaction`. Browser integrations may request
`wallet_switchEthereumChain` for the source-token chain first. If the user
rejects the switch, do not sign the transaction.

1. Send `POST /v1/agent/turn` with the user's natural-language swap request,
   public `address`, `chain`, and `metadata.wallet_chain_id`. Retain the
   returned `run_id`, `conversation_id`, and `session_id`.
   If the service returns a clarification, send the missing information in a
   follow-up turn using the same `conversation_id` and `session_id`.
2. Read `GET /v1/agent/stream/{run_id}` until `action_required`. The graph
   response/session contains every `quote_candidates` item. Render every quote;
   do not auto-select a provider.
   Direct conversational swap turns also expose an expiring `confirmation_state`
   (`status`, `summary`, `requested_at`, `expires_at`) in the session projection;
   render it as an explicit approval step before signing.
3. After the user chooses one card, submit its exact `provider_reference` to
   `POST /v1/swap/{session_id}/select-quote`:

   ```json
   {"user_id":"wallet-user-42","provider_reference":"bridgers-quote-123"}
   ```

4. If the result has `stage: "approval_required"`, display
   `approval_transaction`. This is unsigned EVM data; the App signs and
   broadcasts it locally. If no approval is needed, a `pending_transaction`
   can be returned directly at `stage: "swap_ready"`.
5. After a successful local approve broadcast, submit its full 32-byte EVM
   hash. The server records a hash but does not trust a client-supplied success
   boolean:

   ```json
   POST /v1/swap/{session_id}/approve-broadcast
   {"user_id":"wallet-user-42","chain":"BASE","approve_tx_hash":"0x...64 hex chars"}
   ```

6. Call `POST /v1/swap/{session_id}/continue` with `user_id`. The agent checks
   the receipt and rereads allowance. It may return `approval_pending`,
   `approval_failed`, or `approval_required`; retry only the continue call as
   appropriate. It returns `stage: "swap_ready"` and `pending_transaction`
   only after allowance is sufficient.
7. Show the final unsigned swap transaction, sign and broadcast it locally,
   then submit `{user_id, chain, tx_hash}` to
   `POST /v1/swap/{session_id}/broadcast`. Poll
   `GET /v1/swap/{session_id}` for provider order status.

Each unsigned EVM transaction includes `chain`, `chain_id`, `to`, `data`, and
`value`. The app must display asset/amount/recipient or spender details and
require its own user confirmation before signing.

## Example initial conversational swap turn

```json
{
  "conversation_id": "stable-device-conversation-id",
  "user_id": "wallet-user-42",
  "message": "把 1 USDC 从 Base 换成 BSC 的 USDT",
  "address": "0x1111111111111111111111111111111111111111",
  "chain": "BASE",
  "metadata": {
    "wallet_chain_id": "0x2105"
  }
}
```

If token contract addresses or decimals are not present in the first message,
the assistant returns a clarification. Send the missing values as the next
message while preserving the returned identifiers:

```json
{
  "conversation_id": "stable-device-conversation-id",
  "session_id": "returned-swap-session-id",
  "user_id": "wallet-user-42",
  "message": "源 Token 是 0x...，目标 Token 是 0x...，两个 Token 都是 6 位小数",
  "address": "0x1111111111111111111111111111111111111111",
  "chain": "BASE",
  "metadata": {
    "wallet_chain_id": "0x2105"
  }
}
```

Reconnects reuse the same `conversation_id` and `session_id`; never create a
new provider order, re-broadcast an approve, or re-run a provider write merely
because an SSE connection dropped. Repeating an identical broadcast hash is
idempotent; a different hash for the same session is rejected.

## Transfer and price requests

For EVM transfers, send the natural-language turn with the public wallet
address and chain. The agent extracts the transfer draft across turns and
returns clarification when the recipient, amount, token contract, or decimals
are missing. A successful response includes `response.kind:
"transfer_prepare"`, `pending_transaction`, and `preflight`. Render failed
checks and warnings before signing. Sign and broadcast only in the App. The
wallet must be on the transfer's `chain` / `chain_id`; request
`wallet_switchEthereumChain` before signing when needed. Transfer hashes are
not sent to the swap provider broadcast endpoint. Query a broadcast hash via
`GET /v1/transactions/{chain}/{tx_hash}`. Token USD prices are advisory quote
context, not a price guarantee; display their source timestamp and refresh
policy to users.

The full capability roadmap is in
[docs/wallet-agent-capability-roadmap.md](wallet-agent-capability-roadmap.md).

## Portfolio and gas assistant

The app can use the natural-language intents `portfolio_query` and
`gas_check`, or call the read-only endpoints directly:

- `GET /v1/wallet/{address}/portfolio?chain=BASE`
- `GET /v1/wallet/{address}/gas?chain=BASE`

Portfolio responses include native/token balances, optional USD values,
`price_status`, and price snapshots. Gas responses include the estimated fee,
native balance, `sufficient`, and `shortfall_raw`. A missing price or RPC
response is reported as unavailable; it is not converted to zero.

Token discovery is available through
`GET /v1/assets?chain=BASE&search=USDC`, or the conversational intent
`asset_discovery`. Results include token contract addresses and decimals.
When multiple providers return the same asset, the service deduplicates by
chain, symbol, and contract address while preserving provider errors.

## Release hardening

- Configure durable checkpoint/session persistence in production.
- Configure `ALLOWED_MODEL_IDS` server-side. The app may send `model_id`, but
  never an API key, base URL, signer, wallet client, seed phrase, or private key.
- Require Bearer authentication and enforce session ownership at the API edge.
- Keep integration tests opt-in with `RUN_INTEGRATION_TESTS=1`; unit tests must
  remain network-free.
