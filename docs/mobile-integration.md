# Mobile integration checklist

The wallet app remains non-custodial: private keys, seed phrases, and signers
never leave the device. The service returns quotes, price context, and unsigned
EVM transactions only. The app presents them to the user, signs locally,
broadcasts through its wallet stack, and submits only a chain-qualified hash.

## Swap with explicit quote selection and approval

1. Send `POST /v1/agent/turn` with a stable `conversation_id`, `user_id`, and
   `metadata.swap_request`. Retain the returned `run_id` and `session_id`.
2. Read `GET /v1/agent/stream/{run_id}` until `action_required`. The graph
   response/session contains every `quote_candidates` item. Render every quote;
   do not auto-select a provider.
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

## Example initial swap turn

```json
{
  "conversation_id": "stable-device-conversation-id",
  "user_id": "wallet-user-42",
  "message": "把 1 USDC 从 Base 换成 BSC 的 USDT",
  "metadata": {
    "swap_request": {
      "source_asset": {"chain":"BASE","symbol":"USDC","decimals":6,"address":"0x..."},
      "destination_asset": {"chain":"BSC","symbol":"USDT","decimals":6,"address":"0x..."},
      "input_amount":"1",
      "input_amount_raw":"1000000",
      "sender_address":"0x...",
      "recipient_address":"0x..."
    }
  }
}
```

Reconnects reuse the same `conversation_id` and `session_id`; never create a
new provider order, re-broadcast an approve, or re-run a provider write merely
because an SSE connection dropped. Repeating an identical broadcast hash is
idempotent; a different hash for the same session is rejected.

## Transfer and price requests

For EVM transfers, send the natural-language turn with the structured transfer
metadata supported by the integration contract, then display the returned
`response.kind: "transfer_prepare"` and `pending_transaction`. Sign and
broadcast only in the App. Token USD prices are advisory quote context, not a
price guarantee; display their source timestamp and refresh policy to users.

## Release hardening

- Configure durable checkpoint/session persistence in production.
- Configure `ALLOWED_MODEL_IDS` server-side. The app may send `model_id`, but
  never an API key, base URL, signer, wallet client, seed phrase, or private key.
- Require Bearer authentication and enforce session ownership at the API edge.
- Keep integration tests opt-in with `RUN_INTEGRATION_TESTS=1`; unit tests must
  remain network-free.
