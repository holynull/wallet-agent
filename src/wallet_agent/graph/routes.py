"""Explicit routing policy for graph transitions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.types import Send


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def route_after_intent(state: dict[str, Any]) -> str:
    intent = state.get("intent", "clarification")
    if state.get("broadcast_tx_hash") and intent in {"swap_status", "swap_prepare"}:
        return "register_broadcast"
    if intent == "wallet_query":
        return "wallet_query"
    if intent == "transfer":
        return "transfer"
    if intent in {"swap_select", "swap_allowance"}:
        return "swap_allowance"
    if intent == "price_query":
        return "price_query"
    if intent == "transaction_status":
        return "transaction_status"
    if intent == "portfolio_query":
        return "portfolio_query"
    if intent == "gas_check":
        return "gas_check"
    if intent == "asset_discovery":
        return "asset_discovery"
    if intent == "swap_quote":
        return "swap_resolve"
    if intent == "swap_prepare":
        return "swap_resolve_prepare"
    if intent == "swap_status":
        return "status_poll"
    return "response"


def route_after_resolution(state: dict[str, Any]) -> str | list[Send]:
    """Fan out quote providers, or continue directly to the confirmation gate."""
    if state.get("intent") == "swap_prepare":
        return "confirmation_request"
    providers = state.get("available_providers", [])
    if not providers:
        return "response"
    # Carry the branch key in the declared request field as well; this keeps
    # the ephemeral Send payload visible to TypedDict state validation.
    raw_request = state.get("request") or {}
    base_request = _jsonable(raw_request)
    if not isinstance(base_request, dict):
        base_request = {}
    return [
        Send(
            "quote_provider",
            {
                "provider_name": name,
                "request": {**base_request, "_provider_name": name},
                "swap_request": _jsonable(state.get("swap_request")),
            },
        )
        for name in providers
    ]


def route_after_quote_provider(state: dict[str, Any]) -> str:
    return "quote_response"


def route_after_confirmation(state: dict[str, Any]) -> str:
    confirmation = state.get("confirmation_state") or {}
    if confirmation.get("status") == "approved":
        return "prepare"
    return "response"


def route_after_status(state: dict[str, Any]) -> str:
    snapshot = state.get("status_snapshot") or {}
    status = snapshot.get("status")
    if status in {"completed", "failed", "refunded", "timed_out", "kyc_required", "cancelled"}:
        return "response"
    if state.get("poll_attempts", 0) >= state.get("max_poll_attempts", 3):
        return "response"
    return "status_poll"
