"""Safe projections for operator-only run inspection."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from wallet_agent.persistence import RunEvent

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "privatekey",
    "seedphrase",
    "mnemonic",
    "secretkey",
    "clientsecret",
    "accesstoken",
    "refreshtoken",
    "password",
    "passphrase",
    "apikey",
    "cookie",
    "signature",
    "signedtransaction",
    "signedtx",
    "rawtransaction",
    "rawtx",
    "unsignedtxhex",
    "txhex",
    "psbt",
    "walletclient",
    "signer",
)
_TRANSACTION_DATA_PARENTS = {
    "transaction",
    "pendingtransaction",
    "approvaltransaction",
    "rawtransaction",
    "signedtransaction",
}


def _canonical_key(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _redact_key(key: object, parent_key: str | None) -> bool:
    canonical = _canonical_key(key)
    if any(part in canonical for part in _SENSITIVE_KEY_PARTS):
        return True
    if canonical in {"data", "calldata", "rawdata", "hex"}:
        return bool(parent_key and parent_key in _TRANSACTION_DATA_PARENTS)
    return False


def sanitize_debug_value(value: Any, *, parent_key: str | None = None) -> Any:
    """Return a JSON-shaped copy with credentials and raw transaction bodies removed."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            clean[key_text] = (
                REDACTED
                if _redact_key(key, parent_key)
                else sanitize_debug_value(child, parent_key=_canonical_key(key))
            )
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize_debug_value(item, parent_key=parent_key) for item in value]
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _event_state(events: Sequence[RunEvent]) -> Mapping[str, Any]:
    for event in reversed(events):
        state = event.payload.get("state")
        if isinstance(state, Mapping):
            return state
    return {}


def _response_state(state: Mapping[str, Any]) -> Mapping[str, Any]:
    response = _mapping(state.get("response"))
    nested = response.get("response")
    return nested if isinstance(nested, Mapping) else response


def _tool_timeline(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for field in ("react_tool_timings", "read_tool_calls"):
        values = state.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, Mapping):
                timeline.append(sanitize_debug_value(value))
    return timeline


def _model_trace(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = state.get("model_trace")
    if not isinstance(values, list):
        return []
    return [
        sanitize_debug_value(value)
        for value in values
        if isinstance(value, Mapping)
    ]


def _errors(events: Sequence[RunEvent], state: Mapping[str, Any]) -> list[Any]:
    errors = [
        sanitize_debug_value(event.payload["error"])
        for event in events
        if event.payload.get("error") is not None
    ]
    response = _response_state(state)
    for field in ("errors", "partial_errors"):
        value = response.get(field) or state.get(field)
        if isinstance(value, list):
            errors.extend(sanitize_debug_value(value))
    return errors


def build_run_summary(
    events: Sequence[RunEvent],
    *,
    artifacts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the at-a-glance section shown by the CLI and debug endpoint."""
    state = _event_state(events)
    response = _response_state(state)
    active_task = _mapping(state.get("active_task"))
    selected_quote = _mapping(state.get("selected_quote"))
    route = _mapping(response.get("route")) or {
        key: state.get(key)
        for key in ("graph_role", "route_mode", "route_reason")
        if state.get(key) is not None
    }
    card_keys = (
        sorted(
            key
            for key in artifacts
            if key not in {"response", "model_trace", "read_only", "react_results"}
        )
        if artifacts
        else []
    )
    event_types = Counter(str(event.payload.get("event") or "unknown") for event in events)
    return sanitize_debug_value(
        {
            "event_count": len(events),
            "event_types": dict(sorted(event_types.items())),
            "intent": state.get("intent") or state.get("forced_intent"),
            "task_stage": state.get("task_stage") or active_task.get("stage"),
            "task_kind": active_task.get("kind"),
            "response_kind": response.get("kind"),
            "response_message": response.get("message"),
            "route": route or None,
            "selected_provider": state.get("selected_provider")
            or selected_quote.get("provider"),
            "selected_quote_reference": selected_quote.get("provider_reference"),
            "broadcast_tx_hash": state.get("broadcast_tx_hash"),
            "broadcast_status": state.get("broadcast_status"),
            "tool_timeline": _tool_timeline(state),
            "models": _model_trace(state),
            "cards": card_keys,
            "errors": _errors(events, state),
        }
    )
