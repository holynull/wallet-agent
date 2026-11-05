from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from wallet_agent.persistence.checkpoints import LazyAsyncMySQLSaver, create_checkpointer


class FakeConnection:
    async def ping(self, reconnect: bool = True):
        del reconnect
        return True


class FakePool:
    def __init__(self, *, acquire_error: Exception | None = None) -> None:
        self.acquire_error = acquire_error
        self.closed = False
        self.waited = False

    @asynccontextmanager
    async def acquire(self):
        if self.acquire_error is not None:
            raise self.acquire_error
        yield FakeConnection()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class FakeSaver:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.setup_calls = 0

    async def setup(self) -> None:
        self.setup_calls += 1


@pytest.mark.asyncio
async def test_mysql_checkpointer_opens_a_recycled_connection_pool(monkeypatch):
    pool = FakePool()
    captured: dict[str, object] = {}

    async def create_pool(**kwargs):
        captured.update(kwargs)
        return pool

    monkeypatch.setattr("asyncmy.create_pool", create_pool)
    monkeypatch.setattr("langgraph.checkpoint.mysql.asyncmy.AsyncMySaver", FakeSaver)

    saver = LazyAsyncMySQLSaver(
        "mysql+asyncmy://user:password@localhost:3306/wallet_agent",
        pool_min_size=2,
        pool_max_size=17,
        pool_recycle_seconds=1234,
    )

    assert await saver.ready() is True
    assert saver._context is pool
    assert saver._saver.setup_calls == 1
    assert captured["host"] == "localhost"
    assert captured["port"] == 3306
    assert captured["user"] == "user"
    assert captured["password"] == "password"
    assert captured["db"] == "wallet_agent"
    assert captured["autocommit"] is True
    assert captured["minsize"] == 2
    assert captured["maxsize"] == 17
    assert captured["pool_recycle"] == 1234

    await saver.aclose()
    assert pool.closed is True
    assert pool.waited is True


@pytest.mark.asyncio
async def test_mysql_checkpointer_reports_not_ready_when_pool_cannot_connect(monkeypatch):
    pool = FakePool(acquire_error=RuntimeError("(0, 'Not connected')"))

    async def create_pool(**_kwargs):
        return pool

    monkeypatch.setattr("asyncmy.create_pool", create_pool)
    monkeypatch.setattr("langgraph.checkpoint.mysql.asyncmy.AsyncMySaver", FakeSaver)

    saver = LazyAsyncMySQLSaver(
        "mysql+asyncmy://user:password@localhost:3306/wallet_agent"
    )

    assert await saver.ready() is False


def test_mysql_checkpointer_rejects_invalid_pool_bounds():
    with pytest.raises(ValueError, match="pool max size"):
        LazyAsyncMySQLSaver(
            "mysql+asyncmy://user:password@localhost:3306/wallet_agent",
            pool_min_size=11,
            pool_max_size=10,
        )


def test_create_checkpointer_passes_mysql_pool_configuration():
    handle = create_checkpointer(
        "mysql+asyncmy://user:password@localhost:3306/wallet_agent",
        mysql_pool_min_size=3,
        mysql_pool_max_size=12,
        mysql_pool_recycle_seconds=900,
    )

    assert isinstance(handle.checkpointer, LazyAsyncMySQLSaver)
    assert handle.checkpointer.pool_min_size == 3
    assert handle.checkpointer.pool_max_size == 12
    assert handle.checkpointer.pool_recycle_seconds == 900
