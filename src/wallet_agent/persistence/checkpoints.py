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


@dataclass
class CheckpointerHandle:
    """Owns the lazily opened database-backed checkpointer."""

    checkpointer: LazyAsyncSqliteSaver

    def close(self) -> None:
        """Compatibility hook; async callers should prefer ``aclose``."""

    async def aclose(self) -> None:
        await self.checkpointer.aclose()


def create_checkpointer(persistence_url: str) -> CheckpointerHandle:
    """Create a durable checkpointer for the configured persistence URL."""
    if persistence_url.startswith(("sqlite://", "sqlite+aiosqlite://")):
        return CheckpointerHandle(LazyAsyncSqliteSaver(_sqlite_path(persistence_url)))
    if persistence_url.startswith(("postgres://", "postgresql://", "postgresql+")):
        raise RuntimeError(
            "PostgreSQL persistence requires the optional langgraph-checkpoint-postgres package"
        )
    raise ValueError(f"unsupported persistence URL: {persistence_url}")
