"""Serializable state contract for the wallet graph."""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired, TypedDict

Intent = Literal[
    "wallet_query",
    "swap_quote",
    "swap_prepare",
    "swap_status",
    "clarification",
    "unsupported",
    "transfer",
    "swap_select",
    "swap_allowance",
    "price_query",
    "transaction_status",
    "portfolio_query",
    "gas_check",
    "asset_discovery",
]


class ConversationState(TypedDict, total=False):
    """Serializable task snapshot exposed to the model and the demo."""

    goal: str
    stage: str
    status: str
    slots: dict[str, Any]
    missing_fields: list[str]
    updated_by: str


class ConfirmationState(TypedDict, total=False):
    """A resumable, expiring approval request owned by the wallet client."""

    action: str
    status: str
    requested_at: str
    expires_at: str
    summary: dict[str, Any]
    reason: str | None
    task_id: str
    task_revision: int
    payload_hash: str


TaskKind = Literal["transfer", "swap"]
TaskStatus = Literal["collecting", "ready", "awaiting_confirmation", "completed", "cancelled"]
SlotSource = Literal["user", "wallet", "resolver", "legacy"]


class ActiveTask(TypedDict, total=False):
    """Canonical durable state for one conversational wallet task."""

    task_id: str
    kind: TaskKind
    status: TaskStatus
    stage: str
    revision: int
    slots: dict[str, Any]
    slot_sources: dict[str, SlotSource]
    missing_fields: list[str]
    updated_by: str


def _merge_quote_candidates(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Append fan-out results while replacing entries enriched by later nodes."""
    incoming_items = [item for item in (incoming or []) if not item.get("__clear__")]
    base = (
        []
        if any(item.get("__clear__") for item in (incoming or []))
        else (existing or [])
    )
    merged: dict[str, dict[str, Any]] = {}
    for item in [*base, *incoming_items]:
        key = str(item.get("provider_reference") or item.get("provider") or len(merged))
        merged[key] = item
    return list(merged.values())


def _merge_errors(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    return [*(existing or []), *(incoming or [])]


def _merge_conversation_history(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Append public user/assistant turns while preserving checkpoint history."""
    return [*(existing or []), *(incoming or [])]


class AgentState(TypedDict, total=False):
    """Checkpoint-safe state. Runtime clients are deliberately not part of this type."""

    conversation_id: str
    user_id: str
    request: dict[str, Any]
    intent: Intent
    predicted_intent: NotRequired[Intent | None]
    response_action: NotRequired[str | None]
    active_task: NotRequired[ActiveTask | None]
    supervisor_decision: NotRequired[dict[str, Any] | None]
    supervisor_output: NotRequired[dict[str, Any] | None]
    conversation_state: NotRequired[ConversationState | None]
    task_stage: NotRequired[str | None]
    wallet_context: NotRequired[dict[str, Any] | None]
    capabilities: NotRequired[dict[str, Any] | None]
    swap_request: NotRequired[dict[str, Any] | None]
    swap_draft: NotRequired[dict[str, Any] | None]
    token_candidates: NotRequired[list[dict[str, Any]]]
    transfer_draft: NotRequired[dict[str, Any] | None]
    missing_fields: NotRequired[list[str]]
    forced_intent: NotRequired[str | None]
    transfer_request: NotRequired[dict[str, Any] | None]
    preflight: NotRequired[dict[str, Any] | None]
    transaction_query: NotRequired[dict[str, Any] | None]
    transaction_status_snapshot: NotRequired[dict[str, Any] | None]
    portfolio_request: NotRequired[dict[str, Any] | None]
    portfolio_snapshot: NotRequired[dict[str, Any] | None]
    gas_request: NotRequired[dict[str, Any] | None]
    gas_snapshot: NotRequired[dict[str, Any] | None]
    asset_query: NotRequired[dict[str, Any] | None]
    asset_snapshot: NotRequired[dict[str, Any] | None]
    price_request: NotRequired[dict[str, Any] | None]
    available_providers: NotRequired[list[str]]
    provider_name: NotRequired[str]
    quote_candidates: Annotated[list[dict[str, Any]], _merge_quote_candidates]
    selected_quote: NotRequired[dict[str, Any] | None]
    swap_session: NotRequired[dict[str, Any] | None]
    pending_transaction: NotRequired[dict[str, Any] | None]
    user_confirmation: NotRequired[dict[str, Any] | None]
    confirmation_state: NotRequired[ConfirmationState | None]
    swap_gas_estimate: NotRequired[dict[str, Any] | None]
    broadcast_tx_hash: NotRequired[str | None]
    broadcast_status: NotRequired[str | None]
    provider_order_ids: NotRequired[dict[str, str]]
    provider_orders: NotRequired[dict[str, dict[str, Any]]]
    status_snapshot: NotRequired[dict[str, Any] | None]
    poll_attempts: NotRequired[int]
    max_poll_attempts: NotRequired[int]
    errors: Annotated[list[dict[str, Any]], _merge_errors]
    response: NotRequired[dict[str, Any] | None]
    tool_result: NotRequired[dict[str, Any] | None]
    messages: NotRequired[list[Any]]
    conversation_history: Annotated[list[dict[str, Any]], _merge_conversation_history]
    route: NotRequired[str | None]
    approval_transaction: NotRequired[dict[str, Any] | None]
    allowance_requirement: NotRequired[dict[str, Any] | None]
    approval_tx_hash: NotRequired[str | None]
    authorization_stage: NotRequired[str | None]
