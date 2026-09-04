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
    DomainModel,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    UnsignedTransaction,
)


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


class SqliteSessionStore:
    """Async SQLite session index for a single service instance or test.

    LangGraph checkpoints remain owned by its configured checkpointer; this
    table stores only the app-facing swap session projection.
    """

    def __init__(self, url: str = "sqlite+aiosqlite:///./wallet_agent.db") -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from .tables import SwapSessionRow

        self.engine = create_async_engine(url)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.row_model = SwapSessionRow

    async def init(self) -> None:
        from .tables import Base

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def save(self, session: SwapSessionRecord) -> SwapSessionRecord:
        async with self.session_factory() as db:
            row = await db.get(self.row_model, session.session_id)
            if row is None:
                row = self.row_model(session_id=session.session_id)
                db.add(row)
            row.user_id = session.user_id
            row.thread_id = session.thread_id
            row.payload = session.model_dump_json()
            await db.commit()
        return session

    async def get(self, session_id: str) -> SwapSessionRecord | None:
        async with self.session_factory() as db:
            row = await db.get(self.row_model, session_id)
            return SwapSessionRecord.model_validate_json(row.payload) if row else None

    async def update(self, session_id: str, **changes: object) -> SwapSessionRecord:
        current = await self.get(session_id)
        if current is None:
            raise KeyError(session_id)
        updated = current.model_copy(update={**changes, "updated_at": datetime.now(timezone.utc)})
        await self.save(updated)
        return updated
