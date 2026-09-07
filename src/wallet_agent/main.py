"""Production application factory.

Provider and model clients are created only when explicitly configured. The
module remains importable in unit-test and documentation environments without
credentials or network access.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from wallet_agent.api import create_app
from wallet_agent.config import Settings
from wallet_agent.graph import build_graph
from wallet_agent.persistence import SqliteSessionStore, initialize_checkpointer
from wallet_agent.providers import BridgersProvider, HttpJsonTransport, OmniBridgeProvider


class IntentOutput(BaseModel):
    intent: str


def build_application(settings: Settings | None = None) -> Any:
    settings = settings or Settings()
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - dependency is locked in production
        raise RuntimeError("langchain-openai is required to run the service") from exc

    api_key = settings.deepseek_api_key or settings.openai_api_key
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY or OPENAI_API_KEY is required")
    model = ChatOpenAI(
        model=settings.openai_model,
        api_key=api_key,
        base_url=settings.openai_base_url,
    ).with_structured_output(IntentOutput)
    providers: dict[str, Any] = {}
    transports: list[HttpJsonTransport] = []
    if settings.bridgers_enabled:
        if not settings.bridgers_base_url:
            raise ValueError("BRIDGERS_BASE_URL is required when BRIDGERS_ENABLED=true")
        transport = HttpJsonTransport(
            settings.bridgers_base_url, timeout_seconds=settings.http_timeout_seconds
        )
        transports.append(transport)
        providers["bridgers"] = BridgersProvider.from_transport(
            transport, source_flag=settings.bridgers_source_flag
        )
    if settings.omnibridge_enabled:
        if not settings.omnibridge_base_url:
            raise ValueError("OMNIBRIDGE_BASE_URL is required when OMNIBRIDGE_ENABLED=true")
        transport = HttpJsonTransport(
            settings.omnibridge_base_url, timeout_seconds=settings.http_timeout_seconds
        )
        transports.append(transport)
        providers["omnibridge"] = OmniBridgeProvider.from_transport(
            transport, source_flag=settings.omnibridge_source_flag
        )
    checkpoint_handle = initialize_checkpointer(settings.persistence_url)
    graph = build_graph(
        model=model,
        providers=providers,
        checkpointer=checkpoint_handle.checkpointer,
        max_poll_attempts=settings.poll_max_attempts,
    )
    session_store = SqliteSessionStore(settings.persistence_url)
    application = create_app(graph=graph, providers=providers, store=session_store)
    application.state.checkpointer_handle = checkpoint_handle
    application.state.transports = transports
    return application


try:  # Keep `uvicorn wallet_agent.main:app` import-safe without .env.
    app = build_application()
except Exception:
    app = create_app()
