"""Real-database checkpoint saver checks.

The offline suite runs on MemorySaver and SQLite, so nothing else exercises the
Postgres or MySQL savers -- a broken Postgres saver shipped once because of
exactly that gap. These tests need a live database, so they are marked
`integration` and skip unless the matching URL is provided:

    TEST_POSTGRES_URL=postgresql://user:pass@127.0.0.1:5433/postgres \
        .venv/bin/pytest -q -m integration tests/persistence/test_savers_integration.py

They assert that channel *values* survive a round trip, not merely that a
checkpoint tuple comes back: the MySQL saver returned an empty checkpoint while
still reporting success, which a presence-only assertion would have missed.
"""

from __future__ import annotations

import os

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from wallet_agent.persistence.checkpoints import create_checkpointer

TARGETS = [
    pytest.param("postgres", "TEST_POSTGRES_URL", id="postgres"),
    pytest.param("mysql", "TEST_MYSQL_URL", id="mysql"),
]


async def _round_trip(url: str, thread_id: str) -> None:
    handle = create_checkpointer(url)
    saver = handle.checkpointer

    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"greeting": {"hello": "world"}}
    version = saver.get_next_version(None, None)
    checkpoint["channel_versions"] = {"greeting": version}
    await saver.aput(
        {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "checkpoint_id": "c1",
            }
        },
        checkpoint,
        {"source": "input", "step": 0},
        {"greeting": version},
    )

    found = await saver.aget_tuple(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    )
    assert found is not None, "the saver did not return the checkpoint it just wrote"
    assert (found.checkpoint or {}).get("channel_values") == {
        "greeting": {"hello": "world"}
    }, "channel values did not survive the round trip"

    await handle.aclose()


async def _session_round_trip(url: str, session_id: str) -> None:
    from wallet_agent.persistence.store import SqlSessionStore, SwapSessionRecord

    store = SqlSessionStore(url)
    try:
        record = SwapSessionRecord(
            session_id=session_id, user_id=session_id, thread_id=f"thread-{session_id}"
        )
        await store.save(record)
        loaded = await store.get(session_id)
        assert loaded is not None, "the session store did not return the row it just wrote"
        assert loaded.session_id == session_id
        await store.delete(session_id, user_id=session_id)
    finally:
        await store.aclose()


@pytest.mark.integration
@pytest.mark.parametrize("flavour,env_name", TARGETS)
@pytest.mark.asyncio
async def test_saver_round_trips_channel_values(flavour: str, env_name: str) -> None:
    url = os.getenv(env_name)
    if not url:
        pytest.skip(f"set {env_name} to run the {flavour} saver check")
    await _round_trip(url, f"saver-{flavour}")


@pytest.mark.integration
@pytest.mark.parametrize("flavour,env_name", TARGETS)
@pytest.mark.asyncio
async def test_session_store_round_trips(flavour: str, env_name: str) -> None:
    url = os.getenv(env_name)
    if not url:
        pytest.skip(f"set {env_name} to run the {flavour} session store check")
    await _session_round_trip(url, f"store-{flavour}")
