"""Failover transports for read-only public or managed RPC endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


class RpcEndpointError(RuntimeError):
    pass


class FailoverJsonRpcTransport:
    """Try each endpoint in order, with bounded retries per endpoint."""

    def __init__(
        self,
        urls: list[str],
        *,
        timeout_seconds: float = 10,
        max_attempts: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.urls = [url.rstrip("/") for url in urls if url]
        if not self.urls:
            raise ValueError("at least one RPC URL is required")
        self.max_attempts = max(1, max_attempts)
        self.client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)
        self._cursor = 0

    async def request(self, method: str, params: list[Any]) -> Any:
        last_error: Exception | None = None
        start = self._cursor
        for offset in range(len(self.urls)):
            index = (start + offset) % len(self.urls)
            url = self.urls[index]
            for _attempt in range(self.max_attempts):
                try:
                    response = await self.client.post(
                        url,
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if isinstance(payload, Mapping) and payload.get("error"):
                        raise RpcEndpointError(str(payload["error"]))
                    self._cursor = index
                    return payload
                except (httpx.HTTPError, ValueError, RpcEndpointError) as exc:
                    last_error = exc
            self._cursor = (index + 1) % len(self.urls)
        raise RpcEndpointError(f"all RPC endpoints failed: {last_error}") from last_error

    async def aclose(self) -> None:
        await self.client.aclose()


class FailoverHttpTransport:
    """Failover GET transport for REST-style chain APIs such as TRON."""

    def __init__(self, urls: list[str], *, timeout_seconds: float = 10) -> None:
        self.urls = [url.rstrip("/") for url in urls if url]
        if not self.urls:
            raise ValueError("at least one HTTP RPC URL is required")
        self.client = httpx.AsyncClient(timeout=timeout_seconds)
        self._cursor = 0

    async def get(self, path: str, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for offset in range(len(self.urls)):
            index = (self._cursor + offset) % len(self.urls)
            try:
                response = await self.client.get(f"{self.urls[index]}{path}", **kwargs)
                response.raise_for_status()
                self._cursor = index
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
        raise RpcEndpointError(f"all HTTP endpoints failed: {last_error}") from last_error

    async def aclose(self) -> None:
        await self.client.aclose()
