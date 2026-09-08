# EVM Wallet Actions and Resumable Swap Authorization Design

## Goal

Extend the existing non-custodial LangGraph wallet agent so a mobile app can
use natural-language chat for EVM native/ERC-20 transfers and for swaps through
Bridgers or OmniBridge. The backend performs only read-only chain checks and
unsigned transaction construction; the app signs and broadcasts every
transaction.

## Scope and invariants

- EVM is the only chain family for transaction generation in this slice.
- Existing TRON/Solana read-only adapters remain unchanged.
- The server never accepts or stores private keys, seed phrases, signers, or
  wallet clients.
- Every generated transaction is returned as an explicit unsigned payload with
  chain, chain ID, recipient (`to`), calldata (`data`), value, and display
  metadata.
- The server must verify the authenticated owner/address relationship before
  checking balances, allowance, or generating a transaction.
- A swap quote is selected only by an explicit `provider_reference` previously
  returned in the quote list; the server never silently changes providers.
- CoinGecko is the first price implementation behind a replaceable price
  provider interface.

## State machine

The checkpointed `AgentState` remains JSON-serializable. New intent values are
`transfer`, `swap_select`, `swap_allowance`, and `price_query`.

```text
chat turn
  ├─ transfer intent
  │    → resolve transfer → validate address/chain
  │    → read native/token balance + fee
  │    → insufficient funds OR unsigned transfer transaction
  │
  ├─ swap quote intent
  │    → resolve assets/amount → query providers in parallel
  │    → query token USD prices → return quote list (no auto-selection)
  │
  └─ selected quote intent (provider_reference required)
       → validate quote belongs to session and has not expired
       → read source balance and provider spender requirement
       → read ERC-20 allowance(owner, spender)
          ├─ allowance sufficient → generate unsigned swap/deposit data
          └─ allowance insufficient → generate unsigned approve data
               → interrupt/action_required: app signs+broadcasts approve
               → app submits approve_tx_hash
               → verify receipt succeeded
               → reread allowance
                  ├─ still insufficient → remain approval_required
                  └─ sufficient → generate unsigned swap/deposit data
```

The approve interrupt is resumable by the existing LangGraph `thread_id` and
business `session_id`. Receipt verification and allowance rereads are always
performed after resume; a client assertion that approval succeeded is never
trusted by itself. All provider writes and broadcast registration remain
idempotent.

## Domain contracts

Add typed models:

- `TransferRequest`: source chain, sender, recipient, optional token asset,
  human amount, raw amount, and optional fee buffer.
- `AllowanceRequirement`: token contract, owner, spender, required raw amount,
  current raw allowance, and chain ID.
- `ApprovalTransaction`: an `UnsignedTransaction` plus token/spender/amount
  display fields.
- `TokenPrice`: provider, asset identity, USD price, timestamp, and optional
  confidence/source metadata.
- `SwapAuthorizationState`: selected provider reference, allowance requirement,
  approval tx hash, approval receipt status, and stage.

Extend `NormalizedQuote` with optional `usd_input_value`,
`usd_expected_output`, `price_snapshots`, and an optional
`allowance_requirement` containing spender and token details. Provider adapters
must populate allowance information when the provider exposes a spender; a
provider that does not require ERC-20 approval leaves it absent.

## EVM adapter contract

Extend the EVM adapter with deterministic read/build methods:

```python
async def get_token_balance(self, asset: Asset, owner: str) -> TokenBalance
async def get_allowance(self, token: Asset, owner: str, spender: str) -> str
async def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None
def build_native_transfer(self, *, from_address: str, to_address: str,
                          amount_raw: str) -> UnsignedTransaction
def build_erc20_transfer(self, *, token: Asset, from_address: str,
                         to_address: str, amount_raw: str) -> UnsignedTransaction
def build_erc20_approve(self, *, token: Asset, owner: str, spender: str,
                        amount_raw: str) -> UnsignedTransaction
```

The adapter uses standard ERC-20 selectors (`transfer`, `approve`,
`allowance`) and validates EVM addresses before encoding. Receipt status must be
`0x1`/`1` to proceed. It must not sign or broadcast.

## API contract

Keep `/v1/agent/turn` and SSE for natural-language interaction. Add explicit
action endpoints so an app can implement the flow without replaying prose:

```text
POST /v1/transfer/{session_id}/prepare
POST /v1/swap/{session_id}/select-quote
POST /v1/swap/{session_id}/approve-broadcast
POST /v1/swap/{session_id}/continue
GET  /v1/prices/token
```

`select-quote` accepts only `provider_reference` and optional user confirmation;
it returns either an approval transaction/action-required response or a ready
swap unsigned transaction. `approve-broadcast` accepts only `chain` and
`approve_tx_hash`, validates the hash format, and records it idempotently.
`continue` verifies the receipt and allowance, then resumes the graph. Repeated
calls are safe and return the current stage. Existing `/broadcast` remains for
provider deposit/swap transaction hashes.

Responses use the existing stable envelope and expose a machine-readable
`stage`: `quote_listed`, `approval_required`, `approval_pending`,
`swap_ready`, `broadcasted`, `completed`, or `failed`.

## Price provider

Define a `TokenPriceProvider` protocol and `CoinGeckoPriceProvider` using the
CoinGecko simple price endpoint. Mapping from chain/token address to CoinGecko
asset IDs is configuration-driven; native symbols may use a configured symbol
map. Cache successful prices for a short TTL, return the observation timestamp,
and classify upstream failures as retryable price errors without blocking a
swap when no USD price is available.

## Errors and safety

Use stable error codes for invalid transfer parameters, unsupported EVM chain,
insufficient native/token balance, insufficient gas balance,
`ALLOWANCE_REQUIRED`, approval receipt failure, approval hash conflict, quote
reference mismatch/expiry, and price provider failure. Never continue from an
approval stage based solely on an app-provided boolean.

## Testing and rollout

Add TDD coverage for:

1. native and ERC-20 unsigned transfer encoding and balance/fee rejection;
2. quote list preserving both providers and requiring explicit reference;
3. sufficient allowance continuing directly to swap preparation;
4. insufficient allowance returning approve calldata and interrupting;
5. failed receipt and insufficient post-approval allowance blocking resume;
6. successful receipt plus sufficient allowance resuming exactly once;
7. quote ownership/expiry and approve hash idempotency/conflict;
8. CoinGecko normalization, caching, and degraded-price behavior;
9. restart/resume with the durable checkpoint and stable API/SSE events.

All tests use deterministic fakes. External CoinGecko/provider/RPC calls are
integration-marked and skipped by default.

