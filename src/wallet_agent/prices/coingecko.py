"""CoinGecko price adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from wallet_agent.domain.models import AgentError, Asset, TokenPrice


def _asset_id(
    asset: Asset,
    *,
    token_id_by_address: dict[str, str],
    native_id_by_symbol: dict[str, str],
) -> str | None:
    if asset.address:
        mapped = token_id_by_address.get(asset.address.lower())
        if mapped:
            return mapped
    mapped = native_id_by_symbol.get(asset.symbol.upper())
    if mapped:
        return mapped
    return None


@dataclass
class _CachedBatch:
    expires_at: float
    payload: dict[str, Any]


class CoinGeckoPriceProvider:
    provider_name = "coingecko"

    def __init__(
        self,
        transport: Any,
        token_id_by_address: dict[str, str] | None = None,
        native_id_by_symbol: dict[str, str] | None = None,
        ttl_seconds: int = 60,
        *,
        api_key: str | None = None,
        base_url: str = "https://pro-api.coingecko.com/api/v3",
    ) -> None:
        self.transport = transport
        self.token_id_by_address = {
            str(k).lower(): str(v) for k, v in (token_id_by_address or {}).items()
        }
        self.native_id_by_symbol = {
            str(k).upper(): str(v) for k, v in (native_id_by_symbol or {}).items()
        }
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.last_error: AgentError | None = None
        self._cache: _CachedBatch | None = None
        self._lock = asyncio.Lock()

    async def get_prices(self, assets: list[Asset]) -> list[TokenPrice]:
        now = asyncio.get_running_loop().time()
        if self._cache and self._cache.expires_at > now:
            self.last_error = None
            return self._build_prices(assets, self._cache.payload)
        async with self._lock:
            now = asyncio.get_running_loop().time()
            if self._cache and self._cache.expires_at > now:
                self.last_error = None
                return self._build_prices(assets, self._cache.payload)
            try:
                payload = await self._fetch_batch(assets)
            except Exception as exc:
                self.last_error = AgentError(
                    code="COINGECKO_UNAVAILABLE",
                    message=str(exc) or "CoinGecko unavailable",
                    retryable=True,
                    details={"provider": "coingecko"},
                )
                return []
            self.last_error = None
            if self.ttl_seconds > 0:
                self._cache = _CachedBatch(expires_at=now + self.ttl_seconds, payload=payload)
            return self._build_prices(assets, payload)

    async def _fetch_batch(self, assets: list[Asset]) -> dict[str, Any]:
        ids: list[str] = []
        for asset in assets:
            asset_id = _asset_id(
                asset,
                token_id_by_address=self.token_id_by_address,
                native_id_by_symbol=self.native_id_by_symbol,
            )
            if asset_id and asset_id not in ids:
                ids.append(asset_id)
        if not ids:
            return {}
        headers = {"x-cg-pro-api-key": self.api_key} if self.api_key else None
        params = {
            "ids": ",".join(ids),
            "vs_currencies": "usd",
            "include_last_updated_at": "true",
        }
        return await self.transport.get("/simple/price", params=params, headers=headers)

    def _build_prices(self, assets: list[Asset], payload: dict[str, Any]) -> list[TokenPrice]:
        prices: list[TokenPrice] = []
        for asset in assets:
            asset_id = _asset_id(
                asset,
                token_id_by_address=self.token_id_by_address,
                native_id_by_symbol=self.native_id_by_symbol,
            )
            if not asset_id:
                continue
            entry = payload.get(asset_id)
            if not isinstance(entry, dict) or entry.get("usd") is None:
                continue
            observed_at = entry.get("last_updated_at")
            prices.append(
                TokenPrice(
                    asset=asset,
                    usd_price=Decimal(str(entry["usd"])),
                    observed_at=(
                        datetime.fromtimestamp(int(observed_at), tz=timezone.utc)
                        if observed_at is not None
                        else None
                    ),
                )
            )
        return prices
