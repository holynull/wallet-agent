"""Durable LangGraph checkpointer selection."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, AsyncIterator, Sequence


def _sqlite_path(url: str) -> str:
    if url in {":memory:", "sqlite:///:memory:", "sqlite+aiosqlite:///:memory:"}:
        return ":memory:"
    for prefix in ("sqlite+aiosqlite:///", "sqlite+aiosqlite://", "sqlite:///", "sqlite://"):
        if url.startswith(prefix):
            path = url[len(prefix) :]
            return path or ":memory:"
    raise ValueError(f"unsupported SQLite persistence URL: {url}")


class LazyAsyncSqliteSaver:
    """Open the official async saver on the event loop that runs the graph."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._saver: Any = None
        self._connection: Any = None

    def get_next_version(self, current: str | None, _channel: Any = None) -> str:
        current_version = 0 if current is None else int(str(current).split(".")[0])
        return f"{current_version + 1:032}.{random.random():016}"

    async def _get(self) -> Any:
        if self._saver is None:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            self._connection = await aiosqlite.connect(self.path)
            self._saver = AsyncSqliteSaver(self._connection)
            await self._saver.setup()
        return self._saver

    async def aget_tuple(self, config: dict[str, Any]) -> Any:
        return await (await self._get()).aget_tuple(config)

    async def aget(self, config: dict[str, Any]) -> Any:
        return await (await self._get()).aget(config)

    async def aput(
        self, config: dict[str, Any], checkpoint: Any, metadata: Any, new_versions: Any
    ) -> Any:
        return await (await self._get()).aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await (await self._get()).aput_writes(config, writes, task_id, task_path)

    async def alist(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        saver = await self._get()
        async for item in saver.alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        await (await self._get()).adelete_thread(thread_id)

    async def aclose(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            self._saver = None


def _postgres_dsn(url: str) -> str:
    """Normalize SQLAlchemy-style PostgreSQL URLs for psycopg."""
    if url.startswith("postgres://"):
        return f"postgresql://{url[len('postgres://') :]}"
    if url.startswith("postgresql+"):
        return f"postgresql://{url.split('://', 1)[1]}"
    if url.startswith("postgresql://"):
        return url
    raise ValueError(f"unsupported PostgreSQL persistence URL: {url}")


class LazyAsyncPostgresSaver:
    """Keep the official PostgreSQL saver open for the application lifetime."""

    def __init__(self, dsn: str) -> None:
        self.dsn = _postgres_dsn(dsn)
        self._context: Any = None
        self._saver: Any = None
        self._open_lock: Any = None

    def get_next_version(self, current: str | None, _channel: Any = None) -> str:
        current_version = 0 if current is None else int(str(current).split(".")[0])
        return f"{current_version + 1:032}.{random.random():016}"

    async def _get(self) -> Any:
        if self._saver is not None:
            return self._saver
        import asyncio

        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        async with self._open_lock:
            if self._saver is None:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                self._context = AsyncPostgresSaver.from_conn_string(self.dsn)
                self._saver = await self._context.__aenter__()
                await self._saver.setup()
        return self._saver

    async def aget_tuple(self, config: dict[str, Any]) -> Any:
        return await (await self._get()).aget_tuple(config)

    async def aget(self, config: dict[str, Any]) -> Any:
        return await (await self._get()).aget(config)

    async def aput(
        self, config: dict[str, Any], checkpoint: Any, metadata: Any, new_versions: Any
    ) -> Any:
        return await (await self._get()).aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await (await self._get()).aput_writes(config, writes, task_id, task_path)

    async def alist(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        saver = await self._get()
        async for item in saver.alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        await (await self._get()).adelete_thread(thread_id)

    async def aclose(self) -> None:
        if self._context is not None:
            await self._context.__aexit__(None, None, None)
            self._context = None
            self._saver = None


@dataclass
class CheckpointerHandle:
    """Owns the lazily opened database-backed checkpointer."""

    checkpointer: Any

    def close(self) -> None:
        """Compatibility hook; async callers should prefer ``aclose``."""

    async def aclose(self) -> None:
        await self.checkpointer.aclose()


def create_checkpointer(persistence_url: str) -> CheckpointerHandle:
    """Create a durable checkpointer for the configured persistence URL."""
    if persistence_url.startswith(("sqlite://", "sqlite+aiosqlite://")):
        return CheckpointerHandle(LazyAsyncSqliteSaver(_sqlite_path(persistence_url)))
    if persistence_url.startswith(("postgres://", "postgresql://", "postgresql+")):
        return CheckpointerHandle(LazyAsyncPostgresSaver(persistence_url))
    raise ValueError(f"unsupported persistence URL: {persistence_url}")
