# Unified ReAct Wallet Agent Design

## Goal

Make the conversational wallet agent a single context-aware ReAct loop. The
language model receives the current request, durable conversation history and
previous tool observations, then chooses and sequences wallet capabilities.
Graph code performs only mechanical tool routing, state/checkpoint handling,
bounded execution and response projection.

## Scope and non-goals

- All ordinary natural-language turns use one ReAct model loop.
- Read-only queries, quote preparation and transfer preparation are exposed as
  tools in the same loop.
- Existing provider adapters, chain adapters, persistence, SSE events and
  explicit confirmation/sign/broadcast HTTP endpoints remain supported.
- Tool implementations may validate arguments and enforce provider/API
  boundaries, but must not classify the user's intent with regular expressions
  or a second classifier model.
- Explicit API lifecycle commands (`select-quote`, confirmation, allowance and
  broadcast) remain deterministic integration boundaries; they are not
  conversational intent routers.

## Graph and state

The normal message path is:

```text
START -> react_agent -> (tool_calls ? react_tools : finalize_response) -> END
                         ^                    |
                         +--------------------+
```

`AgentState.messages` is the canonical reducer-backed transcript. Each turn
adds one `HumanMessage`; each model response and every `ToolMessage` is
preserved in checkpoint state. `react_steps` and `react_tool_signatures` are
bounded observability/loop-guard fields. `response` is projected only after the
last assistant message has no tool calls.

The existing legacy fields remain serializable for HTTP lifecycle APIs and old
checkpoints. They are not used to select a conversational route.

## Model contract

`ModelRouter.invoke_react(messages, tools)` binds the configured ReAct model to
the complete tool schema and records one model trace entry per call. The model's
last `AIMessage.content` is the final natural-language answer. It may ask a
clarifying question by returning an `AIMessage` with no tool calls. Structured
UI suggestions are parsed opportunistically; malformed structured output falls
back to the plain assistant text.

The system prompt tells the model to:

- use the whole transcript and verified `ToolMessage` results;
- resolve short follow-ups such as “以太坊” from the active task/history;
- query wallet balance plus price before answering a holding-value question;
- use symbols/network names and let tools resolve contract addresses/decimals;
- ask the user only for facts that tools cannot discover;
- never claim a fact absent from tool observations.

## Tool boundary

The unified registry contains read tools (`wallet_balance`,
`portfolio_snapshot`, `price_lookup`, `market_details`, `price_history`,
`asset_discovery`, `gas_estimate`, `transaction_status`) and business tools
(`get_swap_quote`, `prepare_transfer`, `query_swap_status`).
`get_swap_quote` only discovers assets and returns candidates. Selecting a quote
and continuing through confirmation, allowance, signing, and broadcast remain
explicit client/API lifecycle actions; there is no conversational
`prepare_swap` tool that can bypass that boundary.
Tools return JSON-safe observations and never silently mutate the transcript.
Signing, confirmation and broadcast remain explicit client/API operations.

## Safety and termination

- Tool arguments are validated at the boundary and unknown tools are rejected
  by `ToolNode`.
- A configurable maximum of 8 model/tool rounds prevents runaway loops.
- Repeated identical tool calls are returned as a deterministic observation and
  terminate with a useful response instead of spinning.
- Tool exceptions become `ToolMessage` errors that the model can explain.
- Checkpointers use the existing configured persistence backend and stable
  `thread_id`; SSE continues to stream progress/model/update events.

## Compatibility

Forced lifecycle inputs used by existing API endpoints continue to enter the
legacy deterministic nodes. Normal chat inputs enter ReAct. Existing response
objects keep their `kind`, `message`, `suggestions`, `errors` and artifact
fields so the demo does not need a breaking API change.

## Acceptance criteria

1. No ordinary chat turn invokes the legacy supervisor/classifier/extractor.
2. A value question can produce a balance tool call, a price tool call and a
   final calculation from the same transcript.
3. A short chain follow-up is resolved by the model using transcript context.
4. Tool messages and prior turns survive checkpoint resume.
5. Existing lifecycle and read-only regression tests remain green.
