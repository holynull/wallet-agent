# Real mobile-call demo

This demo intentionally uses the same live application configuration as the
mobile integration. There is no fake model/provider path in the HTML page.

## Start

1. Copy `.env.example` to `.env`.
2. Set `DEEPSEEK_API_KEY` and at least two RPC endpoints per production chain.
3. Set `BRIDGERS_ENABLED=true` and/or `OMNIBRIDGE_ENABLED=true` only after
   configuring the provider source flag supplied by that provider.
4. Start Docker:

   ```bash
   docker compose up --build
   ```

5. Open `http://localhost:8000/demo/`.

## What the page demonstrates

- Real `POST /v1/agent/turn` with `model_id` and optional `metadata.swap_request`.
- Authenticated streaming with `fetch()` and an SSE frame parser. `fetch` is
  used instead of `EventSource` so a Bearer token can be sent in the header.
- `action_required` quote confirmation and `POST /confirm` resume.
- Display of unsigned transaction/deposit order returned by the provider.
- Local-wallet handoff: sign and broadcast outside this service.
- Idempotent `POST /broadcast` with only `{chain, tx_hash}`.
- Reconnection/session recovery through `conversation_id` and `session_id`.

The browser page is a reference client for mobile developers, not a custodial
wallet. Do not paste a private key, mnemonic, signer, or wallet client into any
field. Public RPC endpoints may be replaced by managed RPC URLs in `.env`.
