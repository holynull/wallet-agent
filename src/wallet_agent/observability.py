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
    tool_name: str | None = None,
    tool_status: str | None = None,
    query_scope: str | None = None,
    resolved_chain_count: int | None = None,
    asset_count: int | None = None,
    response_validation: str | None = None,
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
        "conversation_id": state.get("conversation_id"),
        "read_loop_count": state.get("read_loop_count"),
        "read_tool_name": tool_name,
        "read_tool_status": tool_status,
        "query_scope": query_scope,
        "resolved_chain_count": resolved_chain_count,
        "asset_count": asset_count,
        "response_validation": response_validation,
    }
    logger.info(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
