"""Small async JSON transport shared by external provider adapters."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ProviderResponseError(Exception):
    """An API-level provider response with a structured error code."""

    def __init__(
        self,
        code: str,
        message: str = "Provider request failed",
        *,
        retryable: bool = False,
        category: str | None = None,
    ) -> None:
        self.code = str(code)
        self.message = message
        self.retryable = retryable
        self.category = category or ("retryable" if retryable else "client")
        super().__init__(f"Provider response {self.code}: {self.message}")


def _redacted(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): "[REDACTED]" if _sensitive(str(k)) else _redacted(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    return value


def _sensitive(key: str) -> bool:
    canonical = "".join(c for c in key.lower() if c.isalnum())
    return canonical in {
        "authorization",
        "apikey",
        "apitoken",
        "token",
        "password",
        "privatekey",
        "secret",
        "seedphrase",
        "mnemonic",
    } or any(
        part in canonical for part in ("privatekey", "seedphrase", "accesstoken", "clientsecret")
    )


class HttpJsonTransport:
    """POST JSON with bounded retries for transport failures only."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0, retry_delay_seconds)
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        # Never emit request values (addresses, hashes, or credentials) to logs.
        logger.debug("provider POST %s payload_keys=%s", path, sorted(payload))
        for attempt in range(self.max_attempts):
            try:
                response = await self.client.post(path, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("Provider response must be a JSON object")
                return result
            except httpx.TransportError:
                if attempt + 1 >= self.max_attempts:
                    raise
                if self.retry_delay_seconds:
                    await asyncio.sleep(self.retry_delay_seconds)

        raise RuntimeError("unreachable")

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        logger.debug("provider GET %s param_keys=%s", path, sorted(params or {}))
        for attempt in range(self.max_attempts):
            try:
                response = await self.client.get(path, params=params, headers=headers)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("Provider response must be a JSON object")
                return result
            except httpx.TransportError:
                if attempt + 1 >= self.max_attempts:
                    raise
                if self.retry_delay_seconds:
                    await asyncio.sleep(self.retry_delay_seconds)

        raise RuntimeError("unreachable")

    async def aclose(self) -> None:
        await self.client.aclose()
