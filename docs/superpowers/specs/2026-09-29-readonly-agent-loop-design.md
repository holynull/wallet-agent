# Read-Only Conversational Agent Loop Design

## Goal

Make read-only wallet conversations resolve references naturally and use a bounded model-to-tool loop, while preserving the deterministic transfer, swap, approval, signing, and broadcast workflows.

## Scope

This phase covers price, wallet balance, portfolio, gas, asset discovery, and transaction-status conversations. Quote selection remains on the existing provider fan-out path because it aggregates providers and exposes explicit user-selectable cards. Write operations are not exposed to the read-only loop.

## Architecture

```text
dialogue_manager
  -> deterministic reference resolver
  -> supervisor
  -> read_only_agent
       -> read_tool_node
       -> read_observe
       -> read_only_agent (maximum two tool rounds)
       -> unified response
  -> existing transfer/swap workflow
```

The graph uses LangGraph's `ToolNode` and conditional tool routing pattern, but the registered tools are fixed by code. The model cannot select a tool outside the read-only registry. Existing deterministic routes remain compatibility paths while the loop is introduced incrementally.

## Dialogue References

`DialogueState.referent` stores a compact, JSON-safe object:

```text
{
  kind: wallet | asset | transaction | quote,
  id: chain/symbol/transaction_hash/provider_reference,
  facts: the minimum structured facts already shown to the user
}
```

Reference resolution is deterministic before model classification:

- A follow-up asset name inherits the last price request and replaces only the asset.
- “That transaction” inherits the latest unambiguous chain and hash.
- “Which quote is better?” compares only the displayed quote candidates.
- An ordinal such as “the second one” maps to an existing displayed quote and never approves or prepares it.
- Ambiguous references result in a clarification instead of a guessed tool call.

## Read-Only Loop Contract

State fields:

```text
read_loop_count: integer, starts at 0
read_tool_calls: list of {name, args, status}
read_facts: JSON-safe verified tool observations
```

Termination rules:

- At most two model/tool rounds per user turn.
- A missing or malformed tool call terminates with deterministic clarification.
- A tool failure is observed as a typed error and is not retried by another write path.
- A third requested round terminates with a useful fallback response.
- The loop has no edge to transfer, swap preparation, authorization, signing, or broadcast nodes.

Tool registry:

- `wallet_balance`
- `price_lookup`
- `gas_estimate`
- `asset_discovery`
- `transaction_status`

Tool arguments are validated against existing domain models before execution. Tool results are copied into `read_facts`; live providers and model objects are never stored in checkpoint state.

## Fact Safety

The response boundary validates generated drafts against verified facts:

- Asset, chain, hash, price, amount, and status references must occur in facts.
- `pending`, `processing`, and `unknown` cannot be rendered as confirmed or completed.
- Suggestions cannot exceed a known balance.
- Invalid language, unsupported suggestions, or fact mismatches use deterministic fallback text while retaining the structured card.

## Persistence and Recovery

All loop counters, tool-call records, referents, and facts are JSON serializable and checkpointed under the existing stable `thread_id`. A resumed read loop either continues at the next bounded round or terminates safely when the budget is exhausted. Public history remains complete for card restoration; model prompts continue to use a bounded recent-turn projection.

## Acceptance Criteria

- “BTC price” followed by “what about ETH?” resolves ETH without asking for the full request again.
- A transaction-status follow-up can use the prior chain and hash when unambiguous.
- Quote ordinals select only displayed candidates and never cross the confirmation boundary.
- Read-only tool loops stop at two rounds and never invoke a write node.
- Invalid model facts cannot change transaction status or numeric facts in the user response.
- Existing transfer/swap confirmation, interrupt, resume, broadcast, and persistence tests remain passing.

## Non-Goals

- No free-form ReAct loop for wallet writes.
- No provider integration rewrite.
- No removal of legacy state fields in this phase.
- No large transaction-node rewrite before the read-only boundary is covered by regression tests.
