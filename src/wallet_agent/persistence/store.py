"""Small storage boundary used by the HTTP layer and graph.

The in-memory implementation is intentionally deterministic for tests. A
production deployment can implement the same protocol with SQLAlchemy without
changing API handlers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from pydantic import Field

from wallet_agent.domain.models import (
    DepositOrder,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    UnsignedTransaction,
)

from wallet_agent.domain.models import DomainModel


class SwapSessionRecord(DomainModel):
    session_id: str
    user_id: str
    thread_id: str
    status: str = "created"
    quote: NormalizedQuote | None = None
    pending_transaction: UnsignedTransaction | DepositOrder | None = None
    provider_order: ProviderOrder | None = None
    order_status: NormalizedOrderStatus | None = None
    broadcast_tx_hash: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SessionStore(Protocol):
    async def save(self, session: SwapSessionRecord) -> SwapSessionRecord: ...
    async def get(self, session_id: str) -> SwapSessionRecord | None: ...
    async def update(self, session_id: str, **changes: object) -> SwapSessionRecord: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SwapSessionRecord] = {}

    async def save(self, session: SwapSessionRecord) -> SwapSessionRecord:
        self._sessions[session.session_id] = session
        return session

    async def get(self, session_id: str) -> SwapSessionRecord | None:
        return self._sessions.get(session_id)

    async def update(self, session_id: str, **changes: object) -> SwapSessionRecord:
        current = self._sessions.get(session_id)
        if current is None:
            raise KeyError(session_id)
        changes["updated_at"] = datetime.now(timezone.utc)
        updated = current.model_copy(update=changes)
        self._sessions[session_id] = updated
        return updated
