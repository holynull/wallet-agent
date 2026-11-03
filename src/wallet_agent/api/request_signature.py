"""Shared-key request signing for App-facing HTTP routes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi.responses import JSONResponse


class RequestReplayStore(Protocol):
    async def claim(self, nonce: str, *, ttl_seconds: int) -> bool: ...


class InMemoryRequestReplayStore:
    """Single-process replay protection for tests and local fallback."""

    def __init__(self) -> None:
        self._nonces: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def claim(self, nonce: str, *, ttl_seconds: int) -> bool:
        now = time.time()
        async with self._lock:
            expired = [
                key for key, expires_at in self._nonces.items() if expires_at <= now
            ]
            for key in expired:
                self._nonces.pop(key, None)
            if self._nonces.get(nonce, 0) > now:
                return False
            self._nonces[nonce] = now + ttl_seconds
            return True


class RedisRequestReplayStore:
    """Cross-instance replay protection backed by Redis."""

    def __init__(self, client: Any, *, key_prefix: str = "wallet-agent") -> None:
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")

    def _key(self, nonce: str) -> str:
        digest = hashlib.sha256(nonce.encode()).hexdigest()
        return f"{self.key_prefix}:auth:request-nonce:{digest}"

    async def claim(self, nonce: str, *, ttl_seconds: int) -> bool:
        result = await self.client.set(
            self._key(nonce),
            "1",
            nx=True,
            ex=ttl_seconds,
        )
        return bool(result)


@dataclass(frozen=True)
class SignatureCheck:
    valid: bool
    code: str | None = None
    message: str | None = None
    status_code: int = 401
    key_id: str | None = None


def canonical_request_string(
    *,
    method: str,
    raw_path: str,
    raw_query: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    return "\n".join(
        (
            method.upper(),
            raw_path,
            raw_query,
            timestamp,
            nonce,
            hashlib.sha256(body).hexdigest(),
        )
    )


def request_signature_value(secret: str, canonical: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _header(scope: Mapping[str, Any], name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for raw_name, raw_value in scope.get("headers", []):
        if bytes(raw_name).lower() == target:
            return bytes(raw_value).decode("latin-1")
    return None


def _request_id(scope: Mapping[str, Any]) -> str | None:
    state = scope.get("state") or {}
    value = state.get("request_id")
    return str(value) if value else None


def _is_exempt(scope: Mapping[str, Any]) -> bool:
    if str(scope.get("method") or "").upper() == "OPTIONS":
        return True
    path = str(scope.get("path") or "")
    return (
        path in {"/health", "/ready", "/docs", "/openapi.json", "/redoc"}
        or path.startswith("/debug/")
        or (path.startswith("/v1/swap/") and path.endswith("/timeline"))
        or (
            path.startswith("/v1/agent/conversations/")
            and path.endswith("/runs")
        )
    )


def _signature_error(scope: Mapping[str, Any], check: SignatureCheck) -> JSONResponse:
    error_body: dict[str, Any] = {
        "code": check.code,
        "message": check.message,
    }
    request_id = _request_id(scope)
    if request_id:
        error_body["request_id"] = request_id
    return JSONResponse(status_code=check.status_code, content=error_body)


async def _read_body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            break
        if message.get("type") != "http.request":
            continue
        chunks.append(bytes(message.get("body") or b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


class RequestSignatureMiddleware:
    """Validate App request signatures without changing downstream request bodies."""

    def __init__(
        self,
        app: Any,
        *,
        mode: str,
        secret: str | None,
        keys: Mapping[str, str] | None,
        ttl_seconds: int,
        replay_store: RequestReplayStore,
    ) -> None:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"off", "warn", "enforce"}:
            raise ValueError("request signature mode must be off, warn, or enforce")
        if normalized_mode != "off" and not secret and not keys:
            raise ValueError("request signature secret is required when enabled")
        self.app = app
        self.mode = normalized_mode
        self.secret = secret
        self.keys = {str(key): str(value) for key, value in (keys or {}).items()}
        self.ttl_seconds = ttl_seconds
        self.replay_store = replay_store

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or self.mode == "off"
            or _is_exempt(scope)
            or not str(scope.get("path") or "").startswith("/v1/")
        ):
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)
        body_sent = False

        async def receive_with_body() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }
            return await receive()

        check = await self._check_request(scope, body)
        state = scope.setdefault("state", {})
        state["audit_signature_status"] = (
            "valid" if check.valid else f"{self.mode}:{check.code or 'invalid'}"
        )
        if check.key_id:
            state["audit_signature_key_id"] = check.key_id
        if not check.valid and self.mode == "enforce":
            await _signature_error(scope, check)(scope, receive, send)
            return
        await self.app(scope, receive_with_body, send)

    async def _check_request(
        self,
        scope: Mapping[str, Any],
        body: bytes,
    ) -> SignatureCheck:
        key_id = _header(scope, "x-key-id")
        secret = self.secret
        if self.keys:
            if not key_id:
                return SignatureCheck(
                    valid=False,
                    code="REQUEST_SIGNATURE_KEY_REQUIRED",
                    message="X-Key-Id is required when multiple signature keys are configured.",
                )
            secret = self.keys.get(key_id)
            if not secret:
                return SignatureCheck(
                    valid=False,
                    code="UNKNOWN_SIGNATURE_KEY",
                    message="X-Key-Id does not identify a configured signature key.",
                    key_id=key_id,
                )
        timestamp = _header(scope, "x-timestamp")
        nonce = _header(scope, "x-nonce")
        signature = _header(scope, "x-signature")
        if not timestamp or not nonce or not signature:
            return SignatureCheck(
                valid=False,
                code="REQUEST_SIGNATURE_REQUIRED",
                message="X-Timestamp, X-Nonce, and X-Signature are required.",
            )
        if not 1 <= len(nonce) <= 128:
            return SignatureCheck(
                valid=False,
                code="INVALID_REQUEST_SIGNATURE",
                message="X-Nonce is malformed.",
            )
        try:
            timestamp_ms = int(timestamp)
        except ValueError:
            return SignatureCheck(
                valid=False,
                code="INVALID_REQUEST_SIGNATURE",
                message="X-Timestamp must be Unix milliseconds.",
            )
        now_ms = int(time.time() * 1000)
        ttl_ms = self.ttl_seconds * 1000
        if abs(now_ms - timestamp_ms) > ttl_ms:
            return SignatureCheck(
                valid=False,
                code="STALE_REQUEST_TIMESTAMP",
                message="Request timestamp is outside the accepted window.",
            )

        raw_path = scope.get("raw_path")
        path = (
            bytes(raw_path).decode("latin-1")
            if raw_path
            else str(scope.get("path") or "")
        )
        query = bytes(scope.get("query_string") or b"").decode("latin-1")
        canonical = canonical_request_string(
            method=str(scope.get("method") or ""),
            raw_path=path,
            raw_query=query,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        expected = request_signature_value(str(secret), canonical)
        if not hmac.compare_digest(signature, expected):
            return SignatureCheck(
                valid=False,
                code="INVALID_REQUEST_SIGNATURE",
                message="Request signature is invalid.",
            )
        try:
            claimed = await self.replay_store.claim(
                nonce,
                ttl_seconds=self.ttl_seconds,
            )
        except Exception:
            return SignatureCheck(
                valid=False,
                code="REQUEST_SIGNATURE_UNAVAILABLE",
                message="Request replay protection is unavailable.",
                status_code=503,
            )
        if not claimed:
            return SignatureCheck(
                valid=False,
                code="REPLAYED_REQUEST_NONCE",
                message="Request nonce has already been used.",
            )
        return SignatureCheck(valid=True, key_id=key_id)
