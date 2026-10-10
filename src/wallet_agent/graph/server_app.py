"""Entry point for hosting this graph on LangGraph Server (spike).

`langgraph.json` points here. The server owns persistence, so the factory must
NOT hand the graph a checkpointer of its own -- a compiled-in checkpointer
shadows the platform one and breaks thread management. Wiring `checkpointer=`
through `build_graph(..., checkpointer=False)` is the seam this spike verifies.
"""

from __future__ import annotations

from typing import Any

_GRAPH: Any = None


def graph(config: Any = None) -> Any:  # noqa: ARG001 - server passes the run config
    """Build once per process and hand the same compiled graph to the server."""
    global _GRAPH
    if _GRAPH is None:
        from wallet_agent.main import build_application

        application = build_application()
        _GRAPH = application.state.graph
    return _GRAPH
