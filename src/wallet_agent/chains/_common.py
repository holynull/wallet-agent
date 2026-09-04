"""Small helpers shared by chain adapters."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from wallet_agent.domain.models import Asset, TokenBalance


async def rpc_call(transport: Any, method: str, params: list[Any]) -> Any:
    """Call an injected JSON-RPC transport regardless of its common API spelling."""
    if hasattr(transport, "request"):
        result = transport.request(method, params)
    elif hasattr(transport, "call"):
        result = transport.call(method, params)
    elif callable(transport):
        result = transport(method, params)
    elif hasattr(transport, "post"):
        result = transport.post(
            "/",
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
    else:
        raise TypeError("RPC transport must expose request(), call(), or be callable")
    result = await result if hasattr(result, "__await__") else result
    if isinstance(result, Mapping) and "error" in result:
        raise RuntimeError(str(result["error"]))
    if isinstance(result, Mapping) and "result" in result:
        return result["result"]
    return result


async def http_call(transport: Any, method: str, path: str, **kwargs: Any) -> Any:
    """Call an injected HTTP transport and normalize response envelopes."""
    method = method.lower()
    fn = getattr(transport, method, None)
    if fn is None and hasattr(transport, "request"):
        fn = transport.request
        try:
            result = fn(method.upper(), path, **kwargs)
        except TypeError:
            result = fn(method.upper(), path, kwargs)
    elif fn is not None:
        try:
            result = fn(path, **kwargs)
        except TypeError:
            result = fn(path, kwargs)
    else:
        raise TypeError("HTTP transport must expose get()/post()/request()")
    result = await result if hasattr(result, "__await__") else result
    if hasattr(result, "json") and callable(result.json):
        result = result.json()
        result = await result if hasattr(result, "__await__") else result
    if isinstance(result, Mapping) and "error" in result:
        raise RuntimeError(str(result["error"]))
    return result


def token_balance(asset: Asset, raw: int | str) -> TokenBalance:
    raw_string = str(raw)
    value = Decimal(raw_string) / (Decimal(10) ** asset.decimals)
    return TokenBalance(asset=asset, amount=value, amount_raw=raw_string)


def quantity(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "0")
    return int(text, 16) if text.lower().startswith("0x") else int(text or 0)


def parse_status(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"0x1", "1", "confirmed", "success", "successful"}:
        return "confirmed"
    if text in {"0x0", "0", "failed", "fail", "reverted"}:
        return "failed"
    return "unknown"
