"""Cross-instance run-event and conversation-lock primitives."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

TERMINAL_RUN_STATUSES = frozenset({"complete", "failed", "awaiting_confirmation"})


@dataclass(frozen=True)
class RunEvent:
    event_id: str
    payload: dict[str, Any]


class RunStore(Protocol):
    async def create(self, run_id: str, *, owner_id: str | None = None) -> None: ...
    async def append(self, run_id: str, event: Mapping[str, Any]) -> str: ...
    async def set_status(self, run_id: str, status: str) -> None: ...
    async def get_status(self, run_id: str) -> str | None: ...
    async def read(
        self, run_id: str, *, after_id: str = "0", block_ms: int = 0
    ) -> list[RunEvent] | None: ...
    async def ready(self) -> bool: ...
    async def aclose(self) -> None: ...


class InMemoryRunStore:
    """Deterministic local implementation retaining test compatibility."""

    def __init__(self, runs: dict[str, dict[str, Any]] | None = None) -> None:
        self.runs = runs if runs is not None else {}
        self._conditions: dict[str, asyncio.Condition] = {}

    def _condition(self, run_id: str) -> asyncio.Condition:
        condition = self._conditions.get(run_id)
        if condition is None:
            condition = asyncio.Condition()
            self._conditions[run_id] = condition
        return condition

    async def create(self, run_id: str, *, owner_id: str | None = None) -> None:
        self.runs[run_id] = {
            "status": "queued",
            "events": [],
            "owner_id": owner_id,
        }
        self._condition(run_id)

    async def append(self, run_id: str, event: Mapping[str, Any]) -> str:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        event_id = str(len(run["events"]) + 1)
        stored = {**dict(event), "id": event_id}
        run["events"].append(stored)
        async with self._condition(run_id):
            self._condition(run_id).notify_all()
        return event_id

    async def set_status(self, run_id: str, status: str) -> None:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run["status"] = status
        async with self._condition(run_id):
            self._condition(run_id).notify_all()

    async def get_status(self, run_id: str) -> str | None:
        run = self.runs.get(run_id)
        return str(run["status"]) if run is not None else None

    async def read(
        self, run_id: str, *, after_id: str = "0", block_ms: int = 0
    ) -> list[RunEvent] | None:
        run = self.runs.get(run_id)
        if run is None:
            return None

        def unread() -> list[RunEvent]:
            try:
                cursor = int(str(after_id).split("-", 1)[0])
            except ValueError:
                cursor = 0
            events: list[RunEvent] = []
            for index, item in enumerate(run.get("events", []), start=1):
                # Older callers and tests wrote directly to ``app.state.runs``.
                # Their events have no explicit ID, so preserve list position as
                # the stable replay cursor instead of rejecting the whole run.
                event_id = str(item.get("id") or index)
                try:
                    sequence = int(event_id.split("-", 1)[0])
                except ValueError:
                    sequence = index
                    event_id = str(index)
                if sequence > cursor:
                    events.append(
                        RunEvent(
                            event_id,
                            {key: value for key, value in item.items() if key != "id"},
                        )
                    )
            return events

        events = unread()
        if events or block_ms <= 0 or run.get("status") in TERMINAL_RUN_STATUSES:
            return events
        condition = self._condition(run_id)
        try:
            async with condition:
                await asyncio.wait_for(condition.wait(), timeout=block_ms / 1000)
        except TimeoutError:
            pass
        return unread()

    async def ready(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class RedisRunStore:
    """Redis Streams-backed run log readable from any application instance."""

    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str = "wallet-agent",
        ttl_seconds: int = 86_400,
        max_events: int = 2_000,
    ) -> None:
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")
        self.ttl_seconds = ttl_seconds
        self.max_events = max_events

    def _meta_key(self, run_id: str) -> str:
        return f"{self.key_prefix}:run:{run_id}:meta"

    def _events_key(self, run_id: str) -> str:
        return f"{self.key_prefix}:run:{run_id}:events"

    async def create(self, run_id: str, *, owner_id: str | None = None) -> None:
        meta_key = self._meta_key(run_id)
        await self.client.hset(
            meta_key,
            mapping={"status": "queued", "owner_id": owner_id or ""},
        )
        await self.client.expire(meta_key, self.ttl_seconds)

    async def append(self, run_id: str, event: Mapping[str, Any]) -> str:
        meta_key = self._meta_key(run_id)
        if not await self.client.exists(meta_key):
            raise KeyError(run_id)
        events_key = self._events_key(run_id)
        event_id = await self.client.xadd(
            events_key,
            {"payload": json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"))},
            maxlen=self.max_events,
            approximate=True,
        )
        await self.client.expire(meta_key, self.ttl_seconds)
        await self.client.expire(events_key, self.ttl_seconds)
        return _text(event_id)

    async def set_status(self, run_id: str, status: str) -> None:
        meta_key = self._meta_key(run_id)
        if not await self.client.exists(meta_key):
            raise KeyError(run_id)
        await self.client.hset(meta_key, mapping={"status": status})
        await self.client.expire(meta_key, self.ttl_seconds)

    async def get_status(self, run_id: str) -> str | None:
        value = await self.client.hget(self._meta_key(run_id), "status")
        return _text(value) if value is not None else None

    async def read(
        self, run_id: str, *, after_id: str = "0", block_ms: int = 0
    ) -> list[RunEvent] | None:
        if not await self.client.exists(self._meta_key(run_id)):
            return None
        cursor = _redis_stream_cursor(after_id)
        result = await self.client.xread(
            {self._events_key(run_id): cursor},
            count=200,
            block=block_ms or None,
        )
        events: list[RunEvent] = []
        for _stream, entries in result or []:
            for event_id, fields in entries:
                raw = fields.get("payload") if isinstance(fields, Mapping) else None
                if raw is None and isinstance(fields, Mapping):
                    raw = fields.get(b"payload")
                payload = json.loads(_text(raw))
                events.append(RunEvent(_text(event_id), payload))
        return events

    async def ready(self) -> bool:
        return bool(await self.client.ping())

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()


class LockManager(Protocol):
    def lock(self, resource_id: str) -> Any: ...


class InMemoryLockManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, resource_id: str) -> asyncio.Lock:
        lock = self._locks.get(resource_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[resource_id] = lock
        return lock


class RedisLockManager:
    """Token-owned Redis lease with automatic renewal for graph executions."""

    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str = "wallet-agent",
        lease_seconds: float = 120,
        wait_seconds: float = 30,
        retry_seconds: float = 0.05,
    ) -> None:
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")
        self.lease_seconds = lease_seconds
        self.wait_seconds = wait_seconds
        self.retry_seconds = retry_seconds

    def lock(self, resource_id: str) -> "_RedisLease":
        digest = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()
        return _RedisLease(
            self.client,
            f"{self.key_prefix}:lock:{digest}",
            lease_seconds=self.lease_seconds,
            wait_seconds=self.wait_seconds,
            retry_seconds=self.retry_seconds,
        )


class _RedisLease:
    _RELEASE = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
    end
    return 0
    """
    _RENEW = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('pexpire', KEYS[1], ARGV[2])
    end
    return 0
    """

    def __init__(
        self,
        client: Any,
        key: str,
        *,
        lease_seconds: float,
        wait_seconds: float,
        retry_seconds: float,
    ) -> None:
        self.client = client
        self.key = key
        self.token = str(uuid.uuid4())
        self.lease_ms = max(1_000, int(lease_seconds * 1_000))
        self.wait_seconds = wait_seconds
        self.retry_seconds = retry_seconds
        self._renew_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "_RedisLease":
        deadline = time.monotonic() + self.wait_seconds
        while True:
            if await self.client.set(self.key, self.token, nx=True, px=self.lease_ms):
                self._renew_task = asyncio.create_task(self._renew())
                return self
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for the conversation lock")
            await asyncio.sleep(self.retry_seconds)

    async def __aexit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._renew_task
        await self.client.eval(self._RELEASE, 1, self.key, self.token)

    async def _renew(self) -> None:
        delay = max(0.25, self.lease_ms / 3_000)
        while True:
            await asyncio.sleep(delay)
            renewed = await self.client.eval(
                self._RENEW,
                1,
                self.key,
                self.token,
                self.lease_ms,
            )
            if not renewed:
                return


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _redis_stream_cursor(value: str) -> str:
    if value in {"", "0"}:
        return "0-0"
    parts = value.split("-", 1)
    if not all(part.isdigit() for part in parts):
        return "0-0"
    return value if len(parts) == 2 else f"{value}-0"
