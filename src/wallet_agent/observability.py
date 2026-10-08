"""Safe structured events for wallet graph transitions."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger("wallet_agent.events")


class RuntimeMetrics:
    """Small process-local timing registry for model and tool operations.

    The registry intentionally stores aggregate counters only.  It is safe to
    share between graph instances in one worker, but it is not presented as a
    cross-process metrics backend; deployments can export ``snapshot()`` to a
    real metrics system when needed.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    @staticmethod
    def _bucket() -> dict[str, Any]:
        return {
            "count": 0,
            "error_count": 0,
            "total_elapsed_ms": 0.0,
            "avg_elapsed_ms": 0.0,
            "last_elapsed_ms": 0.0,
        }

    def _record(
        self,
        collection: dict[str, dict[str, Any]],
        name: str,
        *,
        elapsed_ms: float,
        status: str,
        error_code: str | None = None,
        cache_status: str | None = None,
    ) -> None:
        key = str(name or "unknown")
        bucket = collection.setdefault(key, self._bucket())
        elapsed = round(max(0.0, float(elapsed_ms)), 2)
        bucket["count"] += 1
        bucket["total_elapsed_ms"] = round(bucket["total_elapsed_ms"] + elapsed, 2)
        bucket["avg_elapsed_ms"] = round(bucket["total_elapsed_ms"] / bucket["count"], 2)
        bucket["last_elapsed_ms"] = elapsed
        if str(status).lower() not in {"success", "completed", "ok"}:
            bucket["error_count"] += 1
        if error_code:
            codes = bucket.setdefault("error_codes", {})
            codes[str(error_code)] = int(codes.get(str(error_code), 0)) + 1
        if cache_status == "hit":
            bucket["cache_hits"] = int(bucket.get("cache_hits", 0)) + 1
        elif cache_status == "miss":
            bucket["cache_misses"] = int(bucket.get("cache_misses", 0)) + 1

    def record_tool(
        self,
        tool_name: str,
        *,
        elapsed_ms: float,
        status: str = "success",
        error_code: str | None = None,
        cache_status: str | None = None,
    ) -> None:
        with self._lock:
            self._record(
                self._tools,
                tool_name,
                elapsed_ms=elapsed_ms,
                status=status,
                error_code=error_code,
                cache_status=cache_status,
            )
            self._tool_calls += 1

    def record_model(
        self,
        model_id: str,
        *,
        role: str = "unknown",
        elapsed_ms: float,
        status: str = "success",
        error_code: str | None = None,
    ) -> None:
        with self._lock:
            key = str(model_id)
            self._record(
                self._models,
                key,
                elapsed_ms=elapsed_ms,
                status=status,
                error_code=error_code,
            )
            roles = self._models[key].setdefault("roles", {})
            roles[str(role or "unknown")] = int(roles.get(str(role or "unknown"), 0)) + 1
            self._model_calls += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            # JSON round-tripping prevents callers from mutating the live
            # counters and guarantees the debug endpoint is serializable.
            return json.loads(
                json.dumps(
                    {
                        "model_calls": self._model_calls,
                        "tool_calls": self._tool_calls,
                        "models": self._models,
                        "tools": self._tools,
                    }
                )
            )

    def reset(self) -> None:
        with getattr(self, "_lock", threading.RLock()):
            self._models: dict[str, dict[str, Any]] = {}
            self._tools: dict[str, dict[str, Any]] = {}
            self._model_calls = 0
            self._tool_calls = 0


class MetadataCache:
    """Bounded TTL cache for non-volatile, JSON-safe asset metadata only."""

    def __init__(self, *, max_entries: int = 512, ttl_seconds: float = 600) -> None:
        self.max_entries = max(0, int(max_entries))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._lock = threading.RLock()
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @staticmethod
    def _safe_copy(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(str(key))
            if item is None:
                self._misses += 1
                return None
            expires_at, value = item
            if self.ttl_seconds <= 0 or expires_at <= now:
                self._items.pop(str(key), None)
                self._misses += 1
                return None
            self._items.move_to_end(str(key))
            self._hits += 1
            return self._safe_copy(value)

    def set(self, key: str, value: Any) -> None:
        if self.max_entries <= 0 or self.ttl_seconds <= 0:
            return
        safe = self._safe_copy(value)
        with self._lock:
            cache_key = str(key)
            self._items[cache_key] = (time.monotonic() + self.ttl_seconds, safe)
            self._items.move_to_end(cache_key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
                self._evictions += 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._items),
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
            }


def _masked_address(value: Any) -> str | None:
    raw = str(value or "")
    if not raw:
        return None
    if raw.startswith("0x") and len(raw) >= 10:
        return f"{raw[:6]}...{raw[-4:]}"
    return "[REDACTED]"


def wallet_event(
    node: str,
    outcome: str,
    *,
    duration_ms: float,
    state: Mapping[str, Any],
    error_code: str | None = None,
    tool_name: str | None = None,
    tool_status: str | None = None,
    query_scope: str | None = None,
    resolved_chain_count: int | None = None,
    asset_count: int | None = None,
    response_validation: str | None = None,
) -> None:
    """Log only an allowlisted operational projection of graph state."""
    task = state.get("active_task") or {}
    context = state.get("wallet_context") or {}
    payload = {
        "event": "wallet_graph",
        "node": node,
        "outcome": outcome,
        "duration_ms": round(float(duration_ms), 2),
        "task_kind": task.get("kind"),
        "task_revision": task.get("revision"),
        "predicted_intent": state.get("predicted_intent"),
        "route": state.get("route"),
        "wallet_address": _masked_address(context.get("address")),
        "error_code": error_code,
        "conversation_id": state.get("conversation_id"),
        "read_loop_count": state.get("read_loop_count"),
        "read_tool_name": tool_name,
        "read_tool_status": tool_status,
        "query_scope": query_scope,
        "resolved_chain_count": resolved_chain_count,
        "asset_count": asset_count,
        "response_validation": response_validation,
    }
    logger.info(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
