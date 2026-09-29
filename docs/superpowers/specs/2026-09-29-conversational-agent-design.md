# Conversational Wallet Agent Design

## Goal

Make the wallet assistant behave like a natural, context-aware conversational agent while retaining deterministic, user-confirmed transaction execution.

## Current Boundary

The existing LangGraph is a reliable wallet workflow orchestrator. It already owns slot extraction, asset resolution, provider quotes, transaction preparation, authorization, interrupts, and status polling. This design adds a conversation layer around those workflows instead of replacing financial execution with unconstrained model actions.

## Design

### 1. Conversation state

Add a compact, serializable dialogue snapshot with one owner:

```text
dialogue:
  goal: read | transfer | swap | status | unknown
  active_topic: wallet | portfolio | price | gas | transaction | transfer | swap
  referent: the latest user-visible result or selected asset/quote
  language: zh | en
  last_result_kind: response kind from the previous completed turn
```

`active_task` remains the source of truth for transfer and swap slots. Existing legacy fields remain readable during migration, but new nodes must not create another duplicate task representation.

### 2. Dialogue manager

Insert a deterministic-first `dialogue_manager` before capability routing. It receives the current message, dialogue snapshot, active task, and the latest persisted public result. It returns a validated decision:

```text
mode: answer | read | collect | execute | cancel | unsupported
capability: wallet | portfolio | price | gas | transaction | assets | transfer | swap | none
referent: optional reference to previous result
reason: model | deterministic | resume
```

Short contextual follow-ups may be resolved deterministically. Ambiguous or compound requests use the classifier model. A model failure falls back to a user-facing clarification, never to an unsafe execution route.

### 3. Unified response boundary

Every terminal capability node produces structured facts. A single response node converts facts into `AgentResponseDraft` for user-facing text and suggestions. The response model is allowed to summarize only facts supplied by the graph and cannot call tools or create transaction data. Deterministic templates remain the fallback for provider/model failures.

### 4. Execution safety

Read-only tasks may later use a bounded `model -> tool -> observe` loop. Transfer, swap, approval, signing, and broadcasting stay in explicit deterministic workflows with policy validation and `interrupt()` confirmation. No response-generation change may bypass the confirmation boundary.

### 5. Persistence and history

The graph checkpoint remains the durable source for dialogue, task, and facts. Public assistant history continues to contain immutable card artifacts, but the response node owns the response text before checkpoint persistence. History is bounded for model prompts through a compact recent-turn view plus the latest result referent.

## Phase 1 Acceptance Criteria

- All read-only response kinds have a non-empty natural-language message on success.
- “我的余额” followed by “哪几项？” or “有多少 USDC？” uses the previous wallet result without a generic greeting.
- A prior result can be referenced for price, transaction, and portfolio follow-ups when the referent is unambiguous.
- Model-generated text cannot introduce facts not present in graph facts.
- Existing transfer/swap confirmation, interrupt, resume, and broadcast tests remain passing.

## Non-goals

- Replacing the transaction workflow with a free-form ReAct agent.
- Changing provider integrations or wallet signing APIs.
- Removing all legacy state fields in the first phase.

