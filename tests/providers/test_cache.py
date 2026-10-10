"""Per-key single flight: unrelated keys must not queue behind each other."""

import asyncio

import pytest

from wallet_agent.providers.cache import AsyncTTLCache


@pytest.mark.asyncio
async def test_same_key_is_loaded_once():
    cache = AsyncTTLCache(ttl_seconds=60)
    calls = []

    async def loader():
        calls.append("load")
        await asyncio.sleep(0.05)
        return "value"

    results = await asyncio.gather(*(cache.get_or_set("a", loader) for _ in range(5)))

    assert results == ["value"] * 5
    assert calls == ["load"]


@pytest.mark.asyncio
async def test_unrelated_keys_load_in_parallel():
    """Regression: a single lock across the loader serialised every request."""
    cache = AsyncTTLCache(ttl_seconds=60)
    started: set[str] = set()
    both_started = asyncio.Event()

    async def loader(key: str):
        started.add(key)
        if len(started) == 2:
            both_started.set()
        # If the two loaders are serialised this never fires and we time out.
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return key

    values = await asyncio.gather(
        cache.get_or_set("a", lambda: loader("a")),
        cache.get_or_set("b", lambda: loader("b")),
    )

    assert values == ["a", "b"]
    assert started == {"a", "b"}


@pytest.mark.asyncio
async def test_a_failing_loader_is_not_cached():
    cache = AsyncTTLCache(ttl_seconds=60)
    attempts = []

    async def failing():
        attempts.append("fail")
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        await cache.get_or_set("a", failing)

    assert await cache.get_or_set("a", lambda: _ok("recovered")) == "recovered"
    assert len(attempts) == 1


async def _ok(value):
    return value


@pytest.mark.asyncio
async def test_zero_ttl_never_caches():
    cache = AsyncTTLCache(ttl_seconds=0)
    calls = []

    async def loader():
        calls.append("load")
        return "value"

    await cache.get_or_set("a", loader)
    await cache.get_or_set("a", loader)

    assert calls == ["load", "load"]


@pytest.mark.asyncio
async def test_clear_drops_entries_and_locks():
    cache = AsyncTTLCache(ttl_seconds=60)
    await cache.get_or_set("a", lambda: _ok("value"))
    await cache.clear()

    assert cache._entries == {}
    assert cache._locks == {}
