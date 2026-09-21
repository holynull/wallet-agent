"""Per-asset OKX-primary/CoinGecko-fallback price composition."""

from __future__ import annotations

import asyncio

from wallet_agent.domain.models import AgentError, Asset, TokenPrice


class CompositePriceProvider:
    def __init__(self, primary=None, fallback=None):
        self.primary = primary
        self.fallback = fallback
        self.last_error: AgentError | None = None
        self._lock = asyncio.Lock()

    async def get_prices(self, assets: list[Asset]) -> list[TokenPrice]:
        if not assets:
            return []
        primary_prices: list[TokenPrice] = []
        try:
            if self.primary is not None:
                primary_prices = await self.primary.get_prices(assets)
        except Exception:
            primary_prices = []
        by_key = {self._key(item.asset): item for item in primary_prices}
        missing = [asset for asset in assets if self._key(asset) not in by_key]
        fallback_prices: list[TokenPrice] = []
        try:
            if self.fallback is not None and missing:
                fallback_prices = await self.fallback.get_prices(missing)
        except Exception:
            fallback_prices = []
        by_key.update({self._key(item.asset): item for item in fallback_prices})
        self.last_error = self._provider_error()
        return [by_key[self._key(asset)] for asset in assets if self._key(asset) in by_key]

    async def get_market_details(self, assets):
        provider = self.primary or self.fallback
        method = getattr(provider, "get_market_details", None)
        if method is None:
            raise RuntimeError("detailed market prices are unavailable")
        return await method(assets)

    async def get_historical_prices(self, asset, **kwargs):
        provider = self.primary or self.fallback
        method = getattr(provider, "get_historical_prices", None)
        if method is None:
            raise RuntimeError("historical prices are unavailable")
        return await method(asset, **kwargs)

    async def get_candles(self, asset, **kwargs):
        provider = self.primary or self.fallback
        method = getattr(provider, "get_candles", None)
        if method is None:
            raise RuntimeError("price candles are unavailable")
        return await method(asset, **kwargs)

    def _provider_error(self):
        for provider in (self.primary, self.fallback):
            error = getattr(provider, "last_error", None)
            if error:
                return error
        return None

    @staticmethod
    def _key(asset):
        return asset.chain.upper(), (asset.address or "").lower(), asset.symbol.upper()
