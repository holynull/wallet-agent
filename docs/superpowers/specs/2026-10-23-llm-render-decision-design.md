# LLM Render Decision for ReAct Cards

## Goal

Let the language model own every *judgment* about user intent and card
rendering, and keep deterministic code for *execution* only. Replaces the
keyword/regular-expression render rules (`_swap_recommendation_desired`,
`_react_swap_pair_from_message`, `_react_swap_continuation`,
`_react_established_swap_pair`, `_looks_like_transfer_message`) with one
bounded, schema-validated model decision, without changing the App-facing API
contract.

## Background and problem

The App renders cards from structured suggestions, not prose. When the ReAct
model answered in prose without a structured surface, `finalize_response`
decided *what card to show* with hand-written rules. Those rules were a
whack-a-mole: each new phrasing that drifted from the keyword list produced a
wrong card, and the fix was to add another keyword.

Concrete failure (2026-10-23, App-side report): after a signed swap the user
asked “到账了没有？”, and the `_swap_recommendation_desired` marker list did not
cover “到账”, so the assistant popped the “好的，想换什么？” recommendation card
instead of reporting the swap status. No marker list can be exhaustive: Chinese
phrasings (“钱到了没”, “到哪一步了”, “收到没”) are open-ended, and each added
marker also widens the false-positive surface (e.g. a “状态” marker can misfire
on unrelated questions).

## Design principle

- **All judgment goes to the model.** “Is this an open-ended swap?” “Does this
  bare ‘兑换’ continue an earlier pair?” “Is this a swap or a transfer?” are
  intent questions the model answers in a strict schema.
- **All execution stays in code.** Balance reads, pair recommendation, card
  JSON assembly, canonicalization, and schema validation are deterministic.
  The model never formats a card.
- **The boundary is a validated schema.** A malformed or drifting decision is
  caught by validation and degrades to prose-only — never to a wrong card.

This matches the existing spec intent that intent must not be classified with
regular expressions or a second free-text classifier.

## Mechanism

### 1. `ReactRenderDecision` contract

`src/wallet_agent/models/contracts.py` — a strict
`UnderstandingModel` (`extra="forbid"`):

```python
class ReactRenderDecision(UnderstandingModel):
    intent: Literal["swap", "transfer", "balance", "status", "price", "other"]
    source_symbol: str | None
    destination_symbol: str | None
    recommend_swap_pairs: bool = False
    reason: str | None
```

`recommend_swap_pairs` is `true` only for an open-ended swap request with no
named pair (“我要兑换”). `source_symbol`/`destination_symbol` are filled only
when the current message names a full pair, or a bare “兑换”/“继续” continues a
pair established in `recent_turns`.

### 2. `ModelRouter.decide_render`

`src/wallet_agent/models/registry.py` — mirrors `classify`: builds a
`ReactRenderDecision`-bound model from the raw classifier, runs a short
bilingual prompt, and returns the validated decision. The prompt explicitly
forbids `recommend_swap_pairs` for arrival/status/fee/slippage questions.

### 3. When the decision runs

`finalize_response` calls `_react_render_decision` only when the turn produced
no self-labeled structured surface:

```text
needs_render_decision =
    no quote
    AND no prepared transfer
    AND no portfolio card
    AND ( prose with no quick replies
          OR transfer_quick_replies fired
          OR an amount keypad lacks slots.intent )
```

So the common paths (balance card, transfer card, quote, labeled keypad) do not
pay an extra model call. The decision only fires where the old keyword rules
used to guess. It is bounded by a 5 s timeout; failure or an unsupported model
returns `None`, which renders prose only (a safe, card-free default).

### 4. Mechanical signals replace the regex on the structured paths

`swap_intent` is derived from:

- a quote result exists,
- `swap_quick_replies` was called,
- `client_action.slots.intent == "swap"` (the model labels keypads; the final
  projection deterministically normalizes a transfer keypad to `intent:"transfer"`
  and a swap keypad to `intent:"swap"`), or
- the render decision says `intent == "swap"`.

`active_pair` comes only from the decision's `source_symbol`/`destination_symbol`.

The prose transfer-collection card (chain → coin → amount) uses the same
boundary. When the model answers a collection step in prose instead of calling
`transfer_quick_replies`, `finalize_response` attaches the next-slot card only
when the render decision says `intent == "transfer"`. Only the slot extraction
(`_react_transfer_slots`) and draft assembly (`_react_transfer_draft`) are
deterministic; the former `_react_transfer_context` regex
(`_looks_like_transfer_message`, “收款地址”/“输入地址” substring scan) no longer
decides the card. `_react_transfer_context` survives only as a structural
signal for `_react_transfer_user_symbol` (telling a user-chosen coin from a
model-defaulted one on a keypad), with the free-text regex branch removed.

### 5. Supervisor (legacy path)

The semantic fallback parsers (`explicit_transfer → transfer`,
`asset_value_query → price_query`, `swap_status_message → swap_status`) are
removed: a non-committal model answer stays as the model decided. The remaining
fallbacks only resume an explicit, already-structured reference (active task,
previous read result, dialogue/quote reference). Hydrating the latest
historical swap's order context for a model-routed `swap_status` is kept as
mechanical state backfill.

## What stays deterministic

- Safety/control-flow guards: `forced_intent` (API), cancel confirmation,
  preflight-balance follow-up, `graph_resume`, checkpoint state.
- Data assembly: `_swap_balance_rows`, `_swap_recommended_pairs`,
  `_swap_pair_chip`/`_swap_entry_chip`, `_swap_source_chain_rows`,
  `_react_transfer_slots`/`_react_transfer_draft` slot reconstruction.
- Schema validation (`ReactRenderDecision.model_validate`,
  `RouteDecision.model_validate`) and the response projection itself.
- Routing hints fed to the supervisor classifier (`explicit_transfer`,
  `asset_value_query`, `historical_swap_status`) remain informational inputs,
  not overrides.

## App API contract

Unchanged. The response keeps `kind` / `message` / `suggestions` /
`client_action` / `portfolio` / `react_results`. The App needs no client-side
change.

## Verification

- `pytest` full offline suite: green.
- `ruff check src tests`: clean; `python3 -m compileall src`: OK.
- `wallet_agent_evals`: 15/15; `wallet_app_evals`: 14/14.
- Behavioral tests were rewritten to inject a model decision and assert the
  rendering: arrival follow-up (`intent=status`) → no recommendation card;
  open-ended “我要兑换” (`recommend_swap_pairs=true`) → recommendation card;
  named pair → no recommendation card but a chain-selection card when the
  source lives on multiple networks; bare “兑换” → continues the established
  pair.

## Tradeoffs and follow-ups

- The decision is a second (small, classifier) model call, but only on prose /
  ambiguous turns, not on the structured happy path.
- Determinism is reduced on that path; tests pin behavior by injecting
  decisions, and schema validation bounds drift.
- If a future change wants fewer calls still, the decision could be folded into
  the main ReAct response contract; that is a separate, larger contract change.
