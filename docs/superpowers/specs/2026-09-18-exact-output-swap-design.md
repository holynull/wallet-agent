# Exact-Output Swap Design

## Goal

Allow a user to request a desired destination amount (for example, “换到 5 USDT”) without silently treating it as the source amount, while preserving the existing exact-input flow and provider safety boundaries.

## Scope

The change covers conversational swap slot extraction, normalized provider quoting, OmniBridge ERC-20 coin-code mapping, and dynamic minimum-amount guidance. It does not create orders or bypass the existing explicit confirmation step.

## Design

### Conversation state

Swap drafts gain two fields:

- `amount_mode`: `exact_in` or `exact_out`.
- `output_amount`: human-readable destination amount for `exact_out` requests.

Existing `input_amount` remains the source/deposit amount for `exact_in`. A token-qualified amount is assigned according to its asset side: source-token amounts populate `input_amount`; destination-token amounts populate `output_amount` and set `amount_mode=exact_out`.

### Provider contract

Providers expose a reverse-quote operation that accepts a normal `SwapQuoteRequest`-like asset/address context plus a desired destination amount and returns a normal forward quote with the calculated source amount. The operation is read-only and must validate provider minimum/maximum source limits.

- OmniBridge calculates an initial source amount from the returned rate, fee rate, and receive-chain fee, clamps/validates provider bounds, then performs a forward quote to verify the output.
- Bridgers performs bounded forward-quote search over the provider's source amount range. It returns the smallest source amount whose quoted output meets the requested destination amount, subject to a finite iteration limit. If no amount satisfies the target, it returns a user-actionable error.

The graph converts a reverse result back into the existing `NormalizedQuote` shape, so preparation, allowance checks, confirmation, and persistence remain unchanged.

### OmniBridge coin codes

Coin codes are derived from the provider asset catalog rather than concatenating the normalized chain name. Native ETH uses `ETH`; ETH ERC-20 assets use catalog values such as `USDC` and `USDT(ERC20)`. The normalized asset model stores the provider code in quote metadata when needed.

### Minimum amount guidance

When a swap is missing a source amount, the graph uses available provider quote bounds to produce a dynamic suggestion. It must not hard-code `1 <token>`. If no provider can return bounds, it falls back to asking for an explicit source amount without claiming a minimum.

### Safety and failure behavior

Reverse quotes are estimates and are revalidated immediately through a forward quote. A rate change, unsupported pair, out-of-range source amount, or inability to meet the requested output produces a clarification/error response; it never creates an order or signs a transaction. Exact-input requests keep their current behavior.

## Verification

Tests cover exact-input regression, exact-output extraction, Omni arithmetic and coin-code payloads, Bridgers bounded search, dynamic minimum suggestions, unsupported/rejected reverse quotes, and the existing confirmation boundary.
