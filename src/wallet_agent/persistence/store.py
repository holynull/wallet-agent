"""Small storage boundary used by the HTTP layer and graph.

The in-memory implementation is intentionally deterministic for tests. A
production deployment can implement the same protocol with SQLAlchemy without
changing API handlers.
"""

from __future__ import annotations

import asyncio
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
    quote_candidates: list[NormalizedQuote] = Field(default_factory=list)
    pending_transaction: UnsignedTransaction | DepositOrder | None = None
    provider_order: ProviderOrder | None = None
    order_status: NormalizedOrderStatus | None = None
    broadcast_tx_hash: str | None = None
    broadcast_status: str | None = None
    stage: str | None = None
    selected_provider_reference: str | None = None
    approval_transaction: dict | None = None
    approval_tx_hash: str | None = None
    allowance_requirement: dict | None = None
    preflight: dict | None = None
    confirmation_state: dict | None = None
    gas_estimate: dict | None = None
    state_schema_version: int = 1
    revision: int = 0
    last_run_id: str | None = None
    last_error: dict | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SessionRevisionConflict(RuntimeError):
    """Raised when a session update was based on a stale revision."""

    def __init__(self, session_id: str, expected: int, actual: int) -> None:
        self.session_id = session_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"session {session_id} revision conflict: expected {expected}, actual {actual}"
        )


class SessionStore(Protocol):
    async def save(self, session: SwapSessionRecord) -> SwapSessionRecord: ...
    async def get(self, session_id: str) -> SwapSessionRecord | None: ...
    async def list_for_user(self, user_id: str, *, limit: int = 50) -> list[SwapSessionRecord]: ...
    async def delete(self, session_id: str, *, user_id: str) -> bool: ...
    async def update(
        self,
        session_id: str,
        *,
        expected_revision: int | None = None,
        **changes: object,
    ) -> SwapSessionRecord: ...
    async def ready(self) -> bool: ...
    async def aclose(self) -> None: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SwapSessionRecord] = {}
        self._lock = asyncio.Lock()

    async def save(self, session: SwapSessionRecord) -> SwapSessionRecord:
        async with self._lock:
            self._sessions[session.session_id] = session.model_copy(deep=True)
            return self._sessions[session.session_id].model_copy(deep=True)

    async def get(self, session_id: str) -> SwapSessionRecord | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            return session.model_copy(deep=True) if session is not None else None

    async def list_for_user(self, user_id: str, *, limit: int = 50) -> list[SwapSessionRecord]:
        async with self._lock:
            sessions = [
                session for session in self._sessions.values() if session.user_id == user_id
            ]
            sessions.sort(key=lambda item: item.updated_at, reverse=True)
            return [session.model_copy(deep=True) for session in sessions[:limit]]

    async def delete(self, session_id: str, *, user_id: str) -> bool:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.user_id != user_id:
                return False
            del self._sessions[session_id]
            return True

    async def update(
        self,
        session_id: str,
        *,
        expected_revision: int | None = None,
        **changes: object,
    ) -> SwapSessionRecord:
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                raise KeyError(session_id)
            if expected_revision is not None and current.revision != expected_revision:
                raise SessionRevisionConflict(session_id, expected_revision, current.revision)
            changes["revision"] = current.revision + 1
            changes["updated_at"] = datetime.now(timezone.utc)
            updated = current.model_copy(update=changes, deep=True)
            self._sessions[session_id] = updated
            return updated.model_copy(deep=True)

    async def ready(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _async_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return f"postgresql+psycopg://{url[len('postgres://') :]}"
    if url.startswith("postgresql://"):
        return f"postgresql+psycopg://{url[len('postgresql://') :]}"
    if url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
        return f"sqlite+aiosqlite://{url[len('sqlite://') :]}"
    return url


class SqlSessionStore:
    """Async SQL session projection store for SQLite or PostgreSQL.

    LangGraph checkpoints remain owned by its configured checkpointer; this
    table stores only the app-facing swap session projection.
    """

    def __init__(self, url: str = "sqlite+aiosqlite:///./wallet_agent.db") -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from .tables import SwapSessionRow

        self.engine = create_async_engine(_async_database_url(url))
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.row_model = SwapSessionRow
        self._initialized = False

    async def init(self) -> None:
        from sqlalchemy import text

        from .tables import Base

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            # ``create_all`` does not add columns to an existing deployment.
            # Keep the tiny projection migration local and idempotent.
            try:
                suffix = " IF NOT EXISTS" if connection.dialect.name == "postgresql" else ""
                await connection.execute(text(
                    f"ALTER TABLE swap_sessions ADD COLUMN{suffix} "
                    "revision INTEGER NOT NULL DEFAULT 0"
                ))
            except Exception:
                pass
        self._initialized = True

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await self.init()

    async def save(self, session: SwapSessionRecord) -> SwapSessionRecord:
        await self._ensure_init()
        async with self.session_factory() as db:
            row = await db.get(self.row_model, session.session_id)
            if row is None:
                row = self.row_model(session_id=session.session_id)
                db.add(row)
            row.user_id = session.user_id
            row.thread_id = session.thread_id
            row.revision = session.revision
            row.payload = session.model_dump_json()
            await db.commit()
        return session

    async def get(self, session_id: str) -> SwapSessionRecord | None:
        await self._ensure_init()
        async with self.session_factory() as db:
            row = await db.get(self.row_model, session_id)
            return SwapSessionRecord.model_validate_json(row.payload) if row else None

    async def list_for_user(self, user_id: str, *, limit: int = 50) -> list[SwapSessionRecord]:
        from sqlalchemy import select

        await self._ensure_init()
        async with self.session_factory() as db:
            rows = await db.execute(
                select(self.row_model)
                .where(self.row_model.user_id == user_id)
            )
            sessions = [
                SwapSessionRecord.model_validate_json(row.payload) for row in rows.scalars()
            ]
            sessions.sort(key=lambda session: session.updated_at, reverse=True)
            return sessions[:limit]

    async def delete(self, session_id: str, *, user_id: str) -> bool:
        from sqlalchemy import delete

        await self._ensure_init()
        async with self.session_factory() as db:
            result = await db.execute(
                delete(self.row_model).where(
                    self.row_model.session_id == session_id,
                    self.row_model.user_id == user_id,
                )
            )
            await db.commit()
            return result.rowcount == 1

    async def update(
        self,
        session_id: str,
        *,
        expected_revision: int | None = None,
        **changes: object,
    ) -> SwapSessionRecord:
        from sqlalchemy import update

        await self._ensure_init()
        async with self.session_factory() as db:
            row = await db.get(self.row_model, session_id)
            if row is None:
                raise KeyError(session_id)
            current = SwapSessionRecord.model_validate_json(row.payload)
            if expected_revision is not None and current.revision != expected_revision:
                raise SessionRevisionConflict(session_id, expected_revision, current.revision)
            updated = current.model_copy(
                update={
                    **changes,
                    "revision": current.revision + 1,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            result = await db.execute(
                update(self.row_model)
                .where(
                    self.row_model.session_id == session_id,
                    self.row_model.revision == current.revision,
                )
                .values(
                    user_id=updated.user_id,
                    thread_id=updated.thread_id,
                    revision=updated.revision,
                    payload=updated.model_dump_json(),
                )
            )
            if result.rowcount != 1:
                await db.rollback()
                latest = await self.get(session_id)
                actual = latest.revision if latest is not None else -1
                raise SessionRevisionConflict(session_id, current.revision, actual)
            await db.commit()
            return updated

    async def ready(self) -> bool:
        from sqlalchemy import text

        await self._ensure_init()
        async with self.session_factory() as db:
            return (await db.execute(text("SELECT 1"))).scalar_one() == 1

    async def aclose(self) -> None:
        await self.engine.dispose()


class SqliteSessionStore(SqlSessionStore):
    """Backward-compatible name for existing imports."""
