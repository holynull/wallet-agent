# Mobile integration checklist

The wallet app remains non-custodial: private keys and signers never leave the
device. The service returns quotes and unsigned transaction/deposit data; the
app displays that data, signs locally, broadcasts through its wallet stack, and
submits only `{chain, tx_hash}` to `/v1/swap/{session_id}/broadcast`.

## Request flow

1. `POST /v1/agent/turn` with a stable `conversation_id` and swap parameters in
   `metadata.swap_request`; retain the returned `run_id` and `session_id`.
2. Subscribe to `GET /v1/agent/stream/{run_id}`. On `action_required`, show the
   normalized quote and confirmation UI.
3. `POST /v1/swap/{session_id}/confirm` with the authenticated user context.
   The response contains `pending_transaction` or a deposit order.
4. Sign and broadcast locally. Submit the chain-qualified transaction hash;
   repeating the same hash is safe and idempotent.
5. Poll `GET /v1/swap/{session_id}` or request a status turn until the provider
   reaches a terminal state.

Reconnects should reuse the same `conversation_id`/`session_id`; never rerun a
provider write solely because an SSE connection dropped.

## Release hardening

- Configure `PERSISTENCE_URL` to durable SQLite for local deployments or the
  PostgreSQL checkpointer package in production.
- Configure `ALLOWED_MODEL_IDS` server-side. The app may send `model_id`, but
  never an API key, base URL, signer, wallet client, seed phrase, or private key.
- Require Bearer authentication and enforce session ownership at the API edge.
- Keep integration tests opt-in with `RUN_INTEGRATION_TESTS=1` and sandbox
  endpoints; unit tests must remain network-free.
