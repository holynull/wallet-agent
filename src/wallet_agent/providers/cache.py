"""Small async TTL cache for provider metadata requests.

Only read-only, relatively static provider data should use this cache. Values
are never cached when the loader raises, and concurrent misses for the same
key are coalesced by the lock.
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
        self._lock = asyncio.Lock()

    async def get_or_set(self, key: object, loader: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()
        async with self._lock:
            entry = self._entries.get(key)
            if self.ttl_seconds > 0 and entry is not None and entry[0] > now:
                return entry[1]
            if entry is not None:
                self._entries.pop(key, None)

            value = await loader()
            if self.ttl_seconds > 0:
                if len(self._entries) >= self.max_entries:
                    oldest = min(self._entries, key=lambda item: self._entries[item][0])
                    self._entries.pop(oldest, None)
                self._entries[key] = (time.monotonic() + self.ttl_seconds, value)
            return value

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
