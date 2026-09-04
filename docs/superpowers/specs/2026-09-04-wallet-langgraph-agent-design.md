# Crypto Wallet LangGraph Agent Design

## 1. Goal and scope

Build a Python + LangGraph backend for a crypto-wallet AI assistant that can be integrated into a mobile wallet app through REST and SSE. The first product slice supports:

- wallet basics: token balances, asset discovery, transaction history, and network fee estimates;
- swap intent recognition and quote comparison through Bridgers and OmniBridge;
- transaction preparation for app-local signing and broadcasting;
- transaction-hash submission and provider order tracking;
- resumable conversations and swap sessions.

The backend is non-custodial. It never receives or stores a private key, seed phrase, signer, or wallet client. The app displays transaction details, signs, broadcasts, and sends the resulting hash back to the backend.

Out of scope for the first implementation: backend wallet custody, automatic transfers using the `EVM_PRIVATE_KEY` helper, portfolio advice, arbitrary token transfers, and implementing every chain adapter as production-complete in one iteration.

## 2. Architecture

```text
Mobile Wallet App
       │ REST / SSE
       ▼
FastAPI Agent Service
       │
       ├── LangGraph orchestration
       ├── SwapProvider: Bridgers | OmniBridge
       ├── ChainAdapter: EVM | Tron | Solana | later chain families
       ├── Persistence: checkpoint + swap session index
       └── OpenAI model boundary
```

LangGraph owns state transitions, routing, retries, interrupts, streaming events, and recovery. Provider clients own HTTP contracts and response normalization. Chain adapters own address validation and read-only wallet data. The graph does not import provider-specific response shapes in its routing logic.

### Capability registry

Each chain advertises capabilities independently: address validation, native balance, token balances, transaction history, fee estimate, and transaction status. Unsupported capabilities return a structured `CHAIN_CAPABILITY_UNAVAILABLE` error; the model must not infer or fabricate a result.

Initial adapter targets are EVM, Tron, and Solana. The registry leaves room for Sui/Aptos, XRP, XLM, Waves, and other chains listed by the two providers without making their partial support look complete.

## 3. LangGraph state and flow

The checkpointed state is typed, serializable, and contains only identifiers and user-visible task data:

```python
class AgentState(TypedDict):
    conversation_id: str
    user_id: str
    request: UserRequest
    intent: Literal[
        "wallet_query", "swap_quote", "swap_prepare", "swap_status",
        "clarification", "unsupported",
    ]
    wallet_context: WalletContext | None
    capabilities: CapabilitySnapshot | None
    quote_candidates: list[NormalizedQuote]
    selected_quote: NormalizedQuote | None
    swap_session: SwapSession | None
    pending_transaction: UnsignedTransaction | None
    user_confirmation: Confirmation | None
    broadcast_tx_hash: str | None
    provider_order_ids: dict[str, str]
    status_snapshot: OrderStatus | None
    errors: list[AgentError]
```

Primary routes:

```text
request → intent/parameter extraction → asset + chain resolution
        → capability/address checks
        ├─ wallet_query → ChainAdapter → response
        ├─ swap_quote → parallel Bridgers + OmniBridge → normalized quotes → response
        ├─ swap_prepare → confirmation interrupt → pending transaction/deposit order
        └─ swap_status → provider query → response
```

Provider lifecycles remain explicit:

```text
Bridgers: quote → calldata → app broadcast → submit tx hash → orderId → poll
Omni: quote → create order → platformAddr → app deposit → submit tx hash → poll
```

Every loop has a timeout, maximum attempts, and terminal failure state. A stable LangGraph `thread_id` is used for conversation recovery; a separate `session_id` identifies the business swap.

## 4. App-facing API

```text
POST /v1/agent/turn
GET  /v1/agent/stream/{run_id}
POST /v1/swap/{session_id}/confirm
POST /v1/swap/{session_id}/broadcast
GET  /v1/swap/{session_id}
GET  /v1/wallet/{address}/balances
GET  /v1/wallet/{address}/transactions
GET  /v1/wallet/{address}/fees
```

`/turn` may return `awaiting_confirmation` with a confirmation action. `/confirm` resumes the graph. `/broadcast` accepts only a chain-qualified transaction hash; it never accepts signing material. SSE emits node/state progress, action-required events, messages, and completion. A disconnected client can query the swap session or reconnect to the stream.

Before signing, the app must show source chain, destination chain, token, amount, minimum/expected output, provider fee, network fee, destination address, refund address, and transaction target/data/value when applicable.

## 5. Provider contracts

```python
class SwapProvider(Protocol):
    async def list_assets(self, query: AssetQuery) -> list[Asset]: ...
    async def quote(self, request: SwapQuoteRequest) -> NormalizedQuote: ...
    async def prepare(self, request: SwapPrepareRequest) -> PendingTransaction | DepositOrder: ...
    async def register_broadcast(self, session: SwapSession, tx_hash: str) -> ProviderOrder: ...
    async def get_status(self, order: ProviderOrder) -> NormalizedOrderStatus: ...
```

Bridgers-specific rules:

- request amounts are raw integer wei strings;
- `amountOutMin` comes from the quote and is carried into calldata preparation;
- calldata returns `to`, `data`, and `value`; gas must be estimated separately;
- `generateOrder` runs after broadcast and its real `orderId` is persisted;
- response code `414` is idempotent success only for order upload;
- API retryable codes are `412`, `413`, `415`, and `777`, plus bounded transport retries.

OmniBridge-specific rules:

- quote uses `getBaseInfo` and validates `depositMin`/`depositMax`;
- create order returns `platformAddr` and an order ID before the app deposit;
- `modifyTxId` associates the app deposit hash with the order;
- `ERROR/error` means processing, not immediate failure;
- refund, KYC, timeout, and receive-complete states are surfaced distinctly.

Normalized quotes and statuses retain a provider reference for support/debugging without leaking raw secrets or full request payloads into logs.

## 6. Security and reliability

- Enforce authenticated user/address ownership; do not trust an arbitrary address in a request body.
- Never log private keys, seed phrases, complete production prompts, or sensitive wallet data.
- Require explicit confirmation before irreversible provider order creation or transaction signing.
- Use idempotency keys for provider writes and application session transitions.
- Retry only transient provider/RPC failures. Do not automatically retry user rejection, validation errors, or unsafe non-idempotent effects.
- Store only serializable checkpoint data. Recreate clients inside nodes.
- Use structured logs with correlation ID, thread ID, session ID, node, duration, outcome, and error class.

## 7. Testing and rollout

Test pure nodes, compiled graph paths, and persistence/integration behavior. Cover normal wallet queries, every route, parallel quote aggregation, unsupported pairs, amount bounds, address mismatch, provider timeouts, user rejection, broadcast failure, duplicate hash submission, Bridgers `414`, Omni processing/refund/KYC/timeout, interrupt resume, SSE reconnect, checkpoint restart, and polling termination.

External APIs are used only in explicit integration tests. Unit tests use deterministic model, provider, and chain-adapter fakes. The first implementation should ship the interfaces and production-safe skeletons for all advertised chain families, with complete read-only adapters added progressively rather than claiming unsupported capability.

## 8. Configuration and dependencies

The implementation will use Python, FastAPI, LangGraph, LangChain/OpenAI integration, an HTTP client, typed validation models, and a checkpoint backend selectable between in-memory tests and PostgreSQL production. Exact package versions must be selected and locked before implementation; API-sensitive code must query the configured Context7 MCP or official documentation first.

Required environment configuration includes OpenAI credentials, Bridgers and Omni source flags, provider timeouts, allowed chains, RPC endpoints, and persistence settings. No private-key setting is required for the non-custodial flow.
