import asyncio

import pytest

from wallet_agent.persistence import (
    InMemoryRunStore,
    RedisLockManager,
    RedisRunStore,
)


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.streams = {}
        self.values = {}

    async def hset(self, key, *, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def expire(self, _key, _seconds):
        return True

    async def exists(self, key):
        return key in self.hashes or key in self.streams or key in self.values

    async def xadd(self, key, fields, **_kwargs):
        entries = self.streams.setdefault(key, [])
        event_id = f"{len(entries) + 1}-0"
        entries.append((event_id, fields))
        return event_id

    async def xread(self, streams, **_kwargs):
        key, cursor = next(iter(streams.items()))
        cursor_number = int(cursor.split("-", 1)[0])
        entries = [
            entry
            for entry in self.streams.get(key, [])
            if int(entry[0].split("-", 1)[0]) > cursor_number
        ]
        return [(key, entries)] if entries else []

    async def ping(self):
        return True

    async def set(self, key, value, *, nx=False, px=None):
        del px
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, _keys, key, token, *args):
        if self.values.get(key) != token:
            return 0
        if "del" in script:
            del self.values[key]
            return 1
        assert args
        return 1


@pytest.mark.asyncio
async def test_in_memory_run_store_assigns_ids_and_replays_after_cursor():
    store = InMemoryRunStore()
    await store.create("run-1", owner_id="alice")

    assert await store.append("run-1", {"event": "progress", "step": 1}) == "1"
    assert await store.append("run-1", {"event": "complete"}) == "2"
    await store.set_status("run-1", "complete")

    replay = await store.read("run-1", after_id="1")
    assert replay is not None
    assert [(event.event_id, event.payload["event"]) for event in replay] == [
        ("2", "complete")
    ]


@pytest.mark.asyncio
async def test_in_memory_run_store_accepts_legacy_events_without_ids():
    runs = {
        "legacy": {
            "status": "complete",
            "events": [{"event": "progress"}, {"event": "complete"}],
        }
    }
    store = InMemoryRunStore(runs)

    replay = await store.read("legacy", after_id="1")

    assert replay is not None
    assert [(event.event_id, event.payload["event"]) for event in replay] == [
        ("2", "complete")
    ]


@pytest.mark.asyncio
async def test_in_memory_run_store_blocking_read_wakes_for_new_event():
    store = InMemoryRunStore()
    await store.create("run-1")
    waiting = asyncio.create_task(store.read("run-1", block_ms=1_000))
    await asyncio.sleep(0)

    await store.append("run-1", {"event": "progress"})

    replay = await asyncio.wait_for(waiting, timeout=0.5)
    assert replay is not None
    assert replay[0].event_id == "1"


@pytest.mark.asyncio
async def test_redis_run_stores_share_and_resume_stream_events():
    redis = FakeRedis()
    producer = RedisRunStore(redis, key_prefix="test")
    consumer = RedisRunStore(redis, key_prefix="test")
    await producer.create("run-1", owner_id="alice")
    first_id = await producer.append("run-1", {"event": "progress"})
    second_id = await producer.append("run-1", {"event": "complete"})
    await producer.set_status("run-1", "complete")

    replay = await consumer.read("run-1", after_id=first_id)
    replay_from_invalid_cursor = await consumer.read("run-1", after_id="not-an-event-id")

    assert second_id == "2-0"
    assert replay is not None
    assert [(event.event_id, event.payload["event"]) for event in replay] == [
        ("2-0", "complete")
    ]
    assert await consumer.get_status("run-1") == "complete"
    assert replay_from_invalid_cursor is not None
    assert len(replay_from_invalid_cursor) == 2
    assert await consumer.ready() is True


@pytest.mark.asyncio
async def test_redis_lock_is_exclusive_and_token_owned():
    redis = FakeRedis()
    manager = RedisLockManager(
        redis,
        key_prefix="test",
        lease_seconds=10,
        wait_seconds=0.01,
        retry_seconds=0.001,
    )
    first = manager.lock("conversation-1")
    await first.__aenter__()
    try:
        with pytest.raises(TimeoutError):
            async with manager.lock("conversation-1"):
                pass
    finally:
        await first.__aexit__(None, None, None)

    async with manager.lock("conversation-1"):
        assert len(redis.values) == 1
    assert redis.values == {}
