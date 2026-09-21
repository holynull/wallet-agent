"""Signed HTTP transport for the documented OKX Onchain OS APIs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .errors import OkxClientError

logger = logging.getLogger(__name__)


class OkxSignedClient:
    """Authenticate and validate OKX API requests without exposing credentials."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        secret_key: str,
        passphrase: str,
        project_id: str | None = None,
        timeout_seconds: float = 15,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], str] | None = None,
        sleep: Callable[[float], Any] | None = None,
        jitter_fn: Callable[[float], float] | None = None,
    ) -> None:
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0, retry_delay_seconds)
        self._api_key = api_key
        self._secret_key = secret_key
        self._passphrase = passphrase
        self._project_id = project_id
        self._clock = clock or self._utc_timestamp
        self._sleep = sleep or asyncio.sleep
        self._jitter_fn = jitter_fn or (lambda base: random.uniform(0, base * 0.1))
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    @staticmethod
    def _utc_timestamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    @staticmethod
    def _query_string(query: Mapping[str, Any] | None) -> str:
        if not query:
            return ""
        # httpx's URL encoder is deterministic, but serializing once here makes
        # the exact signed path equal to the path sent on the wire.
        pairs: list[tuple[str, str]] = []
        for key, value in sorted(query.items(), key=lambda item: str(item[0])):
            if value is None:
                continue
            pairs.append((str(key), str(value)))
        return str(httpx.QueryParams(pairs))

    @staticmethod
    def _body_bytes(body: Any | None) -> bytes:
        if body is None:
            return b""
        return json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code < 600

    @staticmethod
    def _retry_after(headers: httpx.Headers) -> float | None:
        value = headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Any | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        if not path.startswith("/"):
            path = f"/{path}"
        query_string = self._query_string(query)
        request_path = f"{path}?{query_string}" if query_string else path
        body_bytes = self._body_bytes(body)
        for attempt in range(self.max_attempts):
            started = time.monotonic()
            timestamp = self._clock()
            prehash = f"{timestamp}{method}{request_path}{body_bytes.decode('utf-8')}"
            signature = base64.b64encode(
                hmac.new(
                    self._secret_key.encode("utf-8"),
                    prehash.encode("utf-8"),
                    hashlib.sha256,
                ).digest()
            ).decode("ascii")
            headers = {
                "OK-ACCESS-KEY": self._api_key,
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self._passphrase,
            }
            if self._project_id:
                headers["OK-ACCESS-PROJECT"] = self._project_id
            if body is not None:
                headers["Content-Type"] = "application/json"
            try:
                response = await self.client.request(
                    method,
                    request_path,
                    content=body_bytes if body is not None else None,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                logger.debug(
                    "okx request method=%s path=%s status=%s code=%s attempt=%s "
                    "elapsed_ms=%.1f error=%s",
                    method,
                    path,
                    None,
                    None,
                    attempt + 1,
                    (time.monotonic() - started) * 1000,
                    type(exc).__name__,
                )
                if attempt + 1 >= self.max_attempts:
                    raise OkxClientError("transport_error", "OKX transport unavailable") from exc
                await self._wait(None, attempt)
                continue

            response_code = None
            try:
                response_json = response.json()
                if isinstance(response_json, dict):
                    response_code = response_json.get("code")
            except ValueError:
                pass
            logger.debug(
                "okx request method=%s path=%s status=%s code=%s attempt=%s elapsed_ms=%.1f",
                method,
                path,
                response.status_code,
                response_code,
                attempt + 1,
                (time.monotonic() - started) * 1000,
            )
            if self._retryable_status(response.status_code):
                if attempt + 1 >= self.max_attempts:
                    raise OkxClientError(
                        "http_error",
                        "OKX service temporarily unavailable",
                        retryable=True,
                        status_code=response.status_code,
                    )
                await self._wait(self._retry_after(response.headers), attempt)
                continue
            if response.status_code >= 400:
                raise OkxClientError(
                    "http_error",
                    "OKX request was rejected",
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise OkxClientError("invalid_response", "OKX returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise OkxClientError("invalid_response", "OKX response must be an object")
            code = payload.get("code")
            if str(code) != "0":
                raise OkxClientError(
                    str(code or "unknown"),
                    "OKX application request failed",
                    retryable=False,
                    status_code=response.status_code,
                )
            return payload
        raise OkxClientError("retry_exhausted", "OKX request failed after retries", retryable=True)

    async def _wait(self, retry_after: float | None, attempt: int) -> None:
        if retry_after is not None:
            delay = retry_after
        else:
            base = min(self.retry_delay_seconds * (2**attempt), 30.0)
            delay = min(30.0, base + (self._jitter_fn(base) if base else 0))
        if delay:
            await self._sleep(delay)

    async def aclose(self) -> None:
        await self.client.aclose()
