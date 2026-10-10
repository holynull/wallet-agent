"""Push a finished turn's client-facing payload onto the run stream.

Moving the card projection out of the HTTP layer is a prerequisite for hosting
this graph on LangGraph Server: there the platform owns the transport, and
`stream_mode="custom"` is how a node hands structured UI data to the client.

Emission is deliberately best-effort. Outside a streaming context (unit tests,
and the current FastAPI path which does its own projection) LangGraph's writer
is a no-op, and a broken stream must never fail a wallet turn.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

#: Custom event name clients subscribe to (`stream_mode=["custom"]`).
CUSTOM_EVENT = "wallet_agent_card"


def build_custom_payload(response: Mapping[str, Any]) -> dict[str, Any]:
    """Project one finished response into the event the App renders cards from."""
    return {"kind": CUSTOM_EVENT, "response": dict(response)}


def emit_response(
    response: Any, *, writer: Callable[[Any], Any] | None = None
) -> bool:
    """Emit once; returns whether anything was written."""
    if not isinstance(response, Mapping) or not response:
        return False
    if writer is None:
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
        except Exception:
            return False
    if writer is None:
        return False
    try:
        writer(build_custom_payload(response))
    except Exception:
        # A streaming failure must never break the turn itself.
        return False
    return True
