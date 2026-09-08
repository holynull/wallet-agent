# EVM wallet action contract

This reference describes the App-facing EVM transaction boundary. It is
deliberately non-custodial: the backend never accepts private keys, seed
phrases, a signer, wallet client, API key, or RPC credentials from a request.

## Responsibilities

| Service | Wallet App |
| --- | --- |
| Reads balances, allowance, receipts, token prices, and provider quotes | Displays information and obtains user confirmation |
| Returns unsigned transfer, approve, and swap transaction data | Signs with device-held keys and broadcasts |
| Rechecks approval receipt and allowance before swap preparation | Submits only chain plus transaction hashes |

## Swap state progression

`quoted` → `quote_selected` → `approval_required` → `approval_submitted` →
`swap_ready` → `broadcasted`

The approval branch is optional: native assets or already-sufficient ERC-20
allowance proceed from quote selection to `swap_ready`. A submitted approval can
remain `approval_pending`, fail, or leave allowance below the requested amount;
in all three cases no swap transaction is generated.

## Required request order

1. Create a quote session with `POST /v1/agent/turn` and consume its SSE stream.
2. Present all `quote_candidates`; submit the user-selected
   `provider_reference` to `/v1/swap/{session_id}/select-quote`.
3. When returned, locally sign and broadcast `approval_transaction`; submit the
   resulting EVM hash to `/approve-broadcast`.
4. Call `/continue`. Only use `pending_transaction` when stage is `swap_ready`.
5. Locally sign/broadcast the swap and submit its hash to `/broadcast`.

`provider_reference` is a server-issued opaque identifier. The App must return
it unchanged and must not construct or substitute a provider reference.

## Unsigned transaction handling

All transaction objects are displayable, unsigned payloads. For an ERC-20
approve, `to` is the token contract and `data` encodes `approve(spender, amount)`.
For an ERC-20 transfer, `to` is the token contract and `data` encodes
`transfer(recipient, amount)`. Native transfers have `data: "0x"` and their
amount in `value`. The App should validate that the selected chain ID matches
its active network before signing.

## Error and retry policy

- A quote may expire: request fresh quotes and make the user select again.
- Do not treat an approve broadcast as mined. `/continue` is the source of
  truth for receipt success and updated allowance.
- Repeating the same approve or swap hash is safe; changing the recorded hash
  for a session returns a conflict.
- Never retry a provider broadcast with a different transaction hash.
