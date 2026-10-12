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


def _mysql_dsn(url: str) -> str:
    """Normalize MySQL URLs to the asyncmy driver.

    The checkpoint saver and the session store both speak SQLAlchemy URLs, and
    the async path needs an async driver, so a bare ``mysql://`` (or the sync
    ``pymysql`` / ``aiomysql`` drivers) is rewritten to asyncmy. asyncmy has no
    PyMySQL dependency: PyMySQL >= 1.2 replaced ``escape_bytes_prefixed`` with a
    string sentinel, which breaks aiomysql's binary parameter escaping.
    """
    for prefix in ("mysql://", "mysql+pymysql://", "mysql+aiomysql://"):
        if url.startswith(prefix):
            return f"mysql+asyncmy://{url[len(prefix) :]}"
    if url.startswith("mysql+asyncmy://"):
        return url
    raise ValueError(f"unsupported MySQL persistence URL: {url}")


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
    """Keep an official SQL saver open for the application lifetime.

    ``_module`` / ``_class_name`` / ``_normalize`` are overridden by the MySQL
    subclass, so the two drivers share one lifecycle implementation.
    """

    _module = "langgraph.checkpoint.postgres.aio"
    _class_name = "AsyncPostgresSaver"

    def __init__(self, dsn: str) -> None:
        self.dsn = self._normalize(dsn)
        self._context: Any = None
        self._saver: Any = None
        self._open_lock: Any = None

    @staticmethod
    def _normalize(url: str) -> str:
        return _postgres_dsn(url)

    def get_next_version(self, current: str | None, _channel: Any = None) -> str:
        current_version = 0 if current is None else int(str(current).split(".")[0])
        return f"{current_version + 1:032}.{random.random():016}"

    async def _open(self) -> tuple[Any, Any]:
        """Open the saver; drivers with a different connection shape override this."""
        import importlib

        saver_class = getattr(importlib.import_module(self._module), self._class_name)
        context = saver_class.from_conn_string(self.dsn)
        saver = await context.__aenter__()
        await saver.setup()
        return context, saver

    async def _close(self, context: Any) -> None:
        await context.__aexit__(None, None, None)

    async def _get(self) -> Any:
        if self._saver is not None:
            return self._saver
        import asyncio

        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        async with self._open_lock:
            if self._saver is None:
                self._context, self._saver = await self._open()
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
            await self._close(self._context)
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


class LazyAsyncMySQLSaver(LazyAsyncPostgresSaver):
    """MySQL saver (community package, mirrors the official Postgres one).

    ``AsyncMySaver.from_conn_string`` parses the URL with ``urlparse`` and hands
    the *still percent-encoded* password to the driver, so a password containing
    reserved characters is rejected as a wrong password. Open the connection
    here instead, with the password decoded.
    """

    _module = "langgraph.checkpoint.mysql.asyncmy"
    _class_name = "AsyncMySaver"

    @staticmethod
    def _normalize(url: str) -> str:
        return _mysql_dsn(url)

    async def _open(self) -> tuple[Any, Any]:
        import urllib.parse

        import asyncmy
        from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver

        parsed = urllib.parse.urlparse(self.dsn)
        connection = await asyncmy.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=parsed.username,
            password=urllib.parse.unquote(parsed.password or ""),
            db=(parsed.path or "/").lstrip("/").split("?")[0] or None,
            autocommit=True,
            # The saver keeps one connection for the process lifetime, so an idle
            # 8h (the server default) is enough for MySQL to drop it and make the
            # next checkpoint raise "(2006, 'MySQL server has gone away')".
            # Pinging per call would interleave with concurrent queries on the
            # same socket, so keep the session alive instead.
            init_command="SET SESSION wait_timeout=31536000",
        )
        saver = AsyncMySaver(connection)
        await saver.setup()
        return connection, saver

    async def _close(self, context: Any) -> None:
        context.close()
        await context.ensure_closed()


def create_checkpointer(persistence_url: str) -> CheckpointerHandle:
    """Create a durable checkpointer for the configured persistence URL."""
    if persistence_url.startswith(("sqlite://", "sqlite+aiosqlite://")):
        return CheckpointerHandle(LazyAsyncSqliteSaver(_sqlite_path(persistence_url)))
    if persistence_url.startswith(("postgres://", "postgresql://", "postgresql+")):
        return CheckpointerHandle(LazyAsyncPostgresSaver(persistence_url))
    if persistence_url.startswith(("mysql://", "mysql+")):
        return CheckpointerHandle(LazyAsyncMySQLSaver(persistence_url))
    raise ValueError(f"unsupported persistence URL: {persistence_url}")
