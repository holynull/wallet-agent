# Production ReAct Hardening Design

## Goal

Make production routing explicit and observable while preserving the safe
wallet lifecycle boundary. Ordinary natural-language requests must execute in
a conversation graph containing only ReAct nodes; quote selection, confirmation,
authorization, signing, broadcasting, and polling must execute in a separate
lifecycle graph using the same durable checkpoint thread.

## Architecture

```text
GraphRouter (stable API facade)
  ├── conversation_graph: START -> ReAct -> ToolNode -> ReAct -> response
  └── lifecycle_graph: START -> deterministic lifecycle nodes -> END/interrupt
```

Both compiled graphs use the same `AgentState` contract and checkpointer. The
router chooses the conversation graph only for a natural-language message and
chooses the lifecycle graph for a forced intent, a resume `Command`, or an
input without a user message. The API continues to call `ainvoke`, `astream`,
`aget_state`, and `aupdate_state` on the facade, so SSE and persistence do not
need a breaking transport change.

Production startup requires the configured model to implement the ReAct
contract. A compatibility flag is available only for tests and migration; it
is never enabled by the production settings default. The selected route and
reason are persisted in state and emitted with run events.

## P0 requirements

- Fail startup or graph construction when strict production mode has no
  `invoke_react`/tool-binding capability; never silently fall back.
- Compile separate conversation and lifecycle graphs and route them through a
  stable facade.
- Preserve one thread id and one checkpointer so a lifecycle interrupt can be
  resumed after a process restart.
- Keep explicit quote selection and wallet side effects outside free-form
  model tool calls.

## P1 requirements

- Add structured route and tool execution telemetry with call id, tool name,
  elapsed milliseconds, success/error code, and model id.
- Make tool failures JSON-safe and bounded by the existing provider timeout;
  repeated calls and total ReAct rounds remain capped.
- Add an offline evaluation matrix covering balance scope, price resolution,
  multi-step calculation, quote lifecycle handoff, follow-up context, and
  strict route behavior.
- Expose route/tool trace in persisted artifacts without exposing secrets or
  model reasoning text.

## P2 requirements

- Add a small, explicit TTL cache for safe read-only tool observations within a
  process. Volatile prices, balances, transaction status, and gas estimates are
  not cached by default; asset metadata may be cached.
- Add configurable ReAct step and tool timeout budgets and emit budget-exhausted
  errors instead of hanging.
- Add aggregate timing counters that can be inspected in tests and health/debug
  tooling, without requiring an external metrics service.

## Non-goals

- The LLM never signs, broadcasts, or silently confirms a transaction.
- No private keys, credentials, or chain client objects are stored in graph
  state or telemetry.
- No change to the existing HTTP request/response schema is required.
