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
]


def _merge_quote_candidates(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Append fan-out results while replacing entries enriched by later nodes."""
    merged: dict[str, dict[str, Any]] = {}
    for item in [*(existing or []), *(incoming or [])]:
        key = str(item.get("provider_reference") or item.get("provider") or len(merged))
        merged[key] = item
    return list(merged.values())


def _merge_errors(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    return [*(existing or []), *(incoming or [])]


class AgentState(TypedDict, total=False):
    """Checkpoint-safe state. Runtime clients are deliberately not part of this type."""

    conversation_id: str
    user_id: str
    request: dict[str, Any]
    intent: Intent
    wallet_context: NotRequired[dict[str, Any] | None]
    capabilities: NotRequired[dict[str, Any] | None]
    swap_request: NotRequired[dict[str, Any] | None]
    transfer_request: NotRequired[dict[str, Any] | None]
    price_request: NotRequired[dict[str, Any] | None]
    available_providers: NotRequired[list[str]]
    provider_name: NotRequired[str]
    available_providers: NotRequired[list[str]]
    provider_name: NotRequired[str]
    quote_candidates: Annotated[list[dict[str, Any]], _merge_quote_candidates]
    selected_quote: NotRequired[dict[str, Any] | None]
    swap_session: NotRequired[dict[str, Any] | None]
    pending_transaction: NotRequired[dict[str, Any] | None]
    user_confirmation: NotRequired[dict[str, Any] | None]
    broadcast_tx_hash: NotRequired[str | None]
    provider_order_ids: NotRequired[dict[str, str]]
    provider_orders: NotRequired[dict[str, dict[str, Any]]]
    status_snapshot: NotRequired[dict[str, Any] | None]
    poll_attempts: NotRequired[int]
    max_poll_attempts: NotRequired[int]
    errors: Annotated[list[dict[str, Any]], _merge_errors]
    response: NotRequired[dict[str, Any] | None]
    route: NotRequired[str | None]
    approval_transaction: NotRequired[dict[str, Any] | None]
    allowance_requirement: NotRequired[dict[str, Any] | None]
    approval_tx_hash: NotRequired[str | None]
    authorization_stage: NotRequired[str | None]
