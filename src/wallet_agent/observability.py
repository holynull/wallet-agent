"""Safe structured events for wallet graph transitions."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger("wallet_agent.events")


def _masked_address(value: Any) -> str | None:
    raw = str(value or "")
    if not raw:
        return None
    if raw.startswith("0x") and len(raw) >= 10:
        return f"{raw[:6]}...{raw[-4:]}"
    return "[REDACTED]"


def wallet_event(
    node: str,
    outcome: str,
    *,
    duration_ms: float,
    state: Mapping[str, Any],
    error_code: str | None = None,
) -> None:
    """Log only an allowlisted operational projection of graph state."""
    task = state.get("active_task") or {}
    context = state.get("wallet_context") or {}
    payload = {
        "event": "wallet_graph",
        "node": node,
        "outcome": outcome,
        "duration_ms": round(float(duration_ms), 2),
        "task_kind": task.get("kind"),
        "task_revision": task.get("revision"),
        "predicted_intent": state.get("predicted_intent"),
        "route": state.get("route"),
        "wallet_address": _masked_address(context.get("address")),
        "error_code": error_code,
    }
    logger.info(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
