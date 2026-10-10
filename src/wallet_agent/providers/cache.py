"""Small async TTL cache for provider metadata requests.

Only read-only, relatively static provider data should use this cache. Values
are never cached when the loader raises.

Concurrent misses are coalesced **per key**: the first caller for a key runs the
loader while the others wait for that same key, and unrelated keys load in
parallel. A single lock held across the loader would serialise every request
behind whichever upstream happened to be slowest.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class AsyncTTLCache(Generic[T]):
    def __init__(self, ttl_seconds: float = 600, *, max_entries: int = 64) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._entries: dict[object, tuple[float, T]] = {}
        # One lock per key; `_lock` only guards the two dicts and is never held
        # across I/O.
        self._locks: dict[object, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    def _fresh(self, key: object) -> tuple[bool, T | None]:
        entry = self._entries.get(key)
        if entry is not None and self.ttl_seconds > 0 and entry[0] > time.monotonic():
            return True, entry[1]
        return False, None

    async def _lock_for(self, key: object) -> asyncio.Lock:
        async with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get_or_set(self, key: object, loader: Callable[[], Awaitable[T]]) -> T:
        hit, value = self._fresh(key)
        if hit:
            return value  # type: ignore[return-value]
        lock = await self._lock_for(key)
        async with lock:
            # Another caller for this key may have filled it while we waited.
            hit, value = self._fresh(key)
            if hit:
                return value  # type: ignore[return-value]
            loaded = await loader()
            if self.ttl_seconds > 0:
                async with self._lock:
                    if len(self._entries) >= self.max_entries:
                        oldest = min(self._entries, key=lambda item: self._entries[item][0])
                        self._entries.pop(oldest, None)
                        self._locks.pop(oldest, None)
                    self._entries[key] = (time.monotonic() + self.ttl_seconds, loaded)
            return loaded

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
            self._locks.clear()
