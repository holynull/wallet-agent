"""Entry point for hosting this graph on LangGraph Server.

`langgraph.json` points here. The server owns persistence and the HTTP
transport, so this assembles **only** the graph's dependencies: building the
whole FastAPI application would also create the session store, the checkpointer
and Redis, and a compiled-in checkpointer would shadow the platform's.

The factory is async on purpose. `Settings()` resolves its env file through
`os.getcwd()`, and the server rejects synchronous blocking calls inside a run,
so construction is handed to a worker thread.
"""

from __future__ import annotations

import asyncio
from typing import Any

_GRAPH: Any = None
_LOCK = asyncio.Lock()


def _build() -> Any:
    from wallet_agent.config import Settings
    from wallet_agent.main import build_runtime_graph

    # checkpointer=False: the platform injects its own per run.
    # split_graphs=False: the platform validates a CompiledGraph, and the split
    # mode returns our router object instead.
    return build_runtime_graph(
        Settings(_env_file=None), checkpointer=False, split_graphs=False
    )


async def graph(config: Any = None) -> Any:  # noqa: ARG001 - server passes the run config
    """Build once per process, off the event loop, and reuse it."""
    global _GRAPH
    if _GRAPH is None:
        async with _LOCK:
            if _GRAPH is None:
                _GRAPH = await asyncio.to_thread(_build)
    return _GRAPH
