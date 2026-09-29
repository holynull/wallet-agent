import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from wallet_agent.persistence import InMemorySessionStore, SqlSessionStore, SwapSessionRecord
from wallet_agent.persistence.store import SessionRevisionConflict


@pytest.mark.asyncio
async def test_session_updates_use_revision_compare_and_set():
    store = InMemorySessionStore()
    await store.save(SwapSessionRecord(session_id="s", user_id="u", thread_id="t"))
    first = await store.get("s")
    assert first is not None

    async def update(status: str):
        return await store.update("s", expected_revision=first.revision, status=status)

    results = await asyncio.gather(update("one"), update("two"), return_exceptions=True)
    assert sum(isinstance(result, SessionRevisionConflict) for result in results) == 1
    current = await store.get("s")
    assert current is not None
    assert current.revision == 1
    assert current.status in {"one", "two"}


@pytest.mark.asyncio
async def test_session_get_returns_snapshot_not_mutable_store_reference():
    store = InMemorySessionStore()
    await store.save(SwapSessionRecord(session_id="s", user_id="u", thread_id="t"))
    current = await store.get("s")
    assert current is not None
    current.status = "mutated-without-update"
    stored = await store.get("s")
    assert stored is not None
    assert stored.status == "created"


@pytest.mark.asyncio
async def test_sql_session_history_is_sorted_by_updated_at_descending(tmp_path):
    store = SqlSessionStore(f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    now = datetime.now(timezone.utc)
    await store.save(
        SwapSessionRecord(
            session_id="older",
            user_id="u",
            thread_id="t-older",
            revision=99,
            updated_at=now - timedelta(minutes=5),
        )
    )
    await store.save(
        SwapSessionRecord(
            session_id="newer",
            user_id="u",
            thread_id="t-newer",
            revision=1,
            updated_at=now,
        )
    )

    sessions = await store.list_for_user("u")
    await store.aclose()

    assert [session.session_id for session in sessions] == ["newer", "older"]
