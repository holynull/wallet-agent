"""OKX current, detailed, historical, and candle price adapter."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from wallet_agent.domain.models import (
    AgentError,
    Asset,
    HistoricalPricePage,
    HistoricalPricePoint,
    PriceCandle,
    TokenMarketDetails,
    TokenPrice,
)

PricePeriod = Literal["1m", "5m", "30m", "1h", "1d"]
CandleBar = str


class PriceProviderError(Exception):
    """Sanitized OKX price provider failure."""

    def __init__(self, message: str, *, code: str = "OKX_PRICE_ERROR", retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(message)

    def to_agent_error(self) -> AgentError:
        return AgentError(
            code=self.code,
            message=str(self),
            retryable=self.retryable,
            details={"provider": "okx"},
        )


class OkxPriceProvider:
    provider_name = "okx"
    _periods = {"1m", "5m", "30m", "1h", "1d"}

    def __init__(
        self,
        client: Any,
        chain_index_by_name: dict[str, str],
        *,
        ttl_seconds: int = 60,
    ) -> None:
        self.client = client
        self.chain_index_by_name = {str(k).upper(): str(v) for k, v in chain_index_by_name.items()}
        self.chain_name_by_index = {v: k for k, v in self.chain_index_by_name.items()}
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.last_error: AgentError | None = None
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get_prices(self, assets: list[Asset]) -> list[TokenPrice]:
        payload = await self._cached_request(
            ("prices", tuple(self._asset_key(asset) for asset in assets)),
            "POST",
            "/api/v6/dex/market/price",
            body=[self._asset_body(asset) for asset in assets],
        )
        rows = self._rows(payload)
        prices: list[TokenPrice] = []
        for asset in assets:
            row = self._find_row(rows, asset)
            if row is None or row.get("price") in (None, ""):
                continue
            prices.append(
                TokenPrice(
                    asset=asset,
                    usd_price=self._decimal(row["price"], "price"),
                    observed_at=self._timestamp(row.get("time", row.get("timestamp"))),
                    provider=self.provider_name,
                )
            )
        return prices

    async def get_market_details(self, assets: list[Asset]) -> list[TokenMarketDetails]:
        payload = await self._cached_request(
            ("details", tuple(self._asset_key(asset) for asset in assets)),
            "POST",
            "/api/v6/dex/market/price-info",
            body=[self._asset_body(asset) for asset in assets],
        )
        rows = self._rows(payload)
        details: list[TokenMarketDetails] = []
        for asset in assets:
            row = self._find_row(rows, asset)
            if row is None:
                continue
            details.append(
                TokenMarketDetails(
                    asset=asset,
                    usd_price=self._optional_decimal(row.get("price"), "price"),
                    market_cap=self._optional_decimal(row.get("marketCap"), "marketCap"),
                    volume_24h=self._optional_decimal(
                        row.get("volume24h", row.get("volume24H")), "volume24h"
                    ),
                    price_change_24h=self._optional_decimal(
                        row.get("priceChange24h", row.get("priceChange24H")), "priceChange24h"
                    ),
                    liquidity=self._optional_decimal(row.get("liquidity"), "liquidity"),
                    holder_count=self._optional_int(row.get("holderCount")),
                )
            )
        return details

    async def get_historical_prices(
        self,
        asset: Asset,
        *,
        period: PricePeriod,
        begin_ms: int | None = None,
        end_ms: int | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> HistoricalPricePage:
        if period not in self._periods:
            raise PriceProviderError(
                "Unsupported historical price period.", code="OKX_INVALID_ARGUMENT"
            )
        if not 1 <= limit <= 200:
            raise PriceProviderError(
                "Historical price limit must be between 1 and 200.",
                code="OKX_INVALID_ARGUMENT",
            )
        query = self._history_query(asset, period, begin_ms, end_ms, cursor, limit)
        payload = await self._cached_request(
            ("history", tuple(sorted(query.items()))),
            "GET",
            "/api/v6/dex/index/historical-price",
            query=query,
        )
        data = payload.get("data")
        if isinstance(data, dict):
            rows = data.get("history", data.get("data", []))
            next_cursor = data.get("cursor", data.get("nextCursor"))
        else:
            rows, next_cursor = data, payload.get("cursor")
        if not isinstance(rows, list):
            raise PriceProviderError("Malformed historical price response.")
        points = [
            HistoricalPricePoint(
                price=self._decimal(item.get("price"), "price"),
                observed_at=self._timestamp(item.get("ts", item.get("time"))),
            )
            for item in rows
            if isinstance(item, dict)
        ]
        if len(points) != len(rows):
            raise PriceProviderError("Malformed historical price response.")
        return HistoricalPricePage(
            points=points, next_cursor=str(next_cursor) if next_cursor else None
        )

    async def get_candles(
        self,
        asset: Asset,
        *,
        bar: CandleBar,
        before_ms: int | None = None,
        after_ms: int | None = None,
        limit: int = 100,
    ) -> list[PriceCandle]:
        if not 1 <= limit <= 299:
            raise PriceProviderError(
                "Candle limit must be between 1 and 299.", code="OKX_INVALID_ARGUMENT"
            )
        query: dict[str, Any] = {
            "chainIndex": self._chain_index(asset),
            "tokenContractAddress": self._candle_address(asset),
            "bar": bar,
            "limit": str(limit),
        }
        if before_ms is not None:
            query["before"] = str(before_ms)
        if after_ms is not None:
            query["after"] = str(after_ms)
        payload = await self._cached_request(
            ("candles", tuple(sorted(query.items()))),
            "GET",
            "/api/v6/dex/market/candles",
            query=query,
        )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise PriceProviderError("Malformed candle response.")
        candles: list[PriceCandle] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                raise PriceProviderError("Malformed candle response.")
            candles.append(
                PriceCandle(
                    observed_at=self._timestamp(row[0]),
                    open=self._decimal(row[1], "open"),
                    high=self._decimal(row[2], "high"),
                    low=self._decimal(row[3], "low"),
                    close=self._decimal(row[4], "close"),
                    volume=self._decimal(row[5], "volume"),
                    volume_usd=self._decimal(row[6], "volumeUsd"),
                    confirmed=str(row[7]) in {"1", "true", "True"},
                )
            )
        return candles

    async def _cached_request(self, key, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            self.last_error = None
            return cached[1]
        async with self._lock:
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                self.last_error = None
                return cached[1]
            try:
                payload = await self.client.request(method, path, **kwargs)
                if not isinstance(payload, dict) or str(payload.get("code")) != "0":
                    raise PriceProviderError("OKX price provider returned an application error.")
            except PriceProviderError:
                raise
            except Exception as exc:
                self.last_error = AgentError(
                    code="OKX_PRICE_UNAVAILABLE",
                    message="OKX price provider is unavailable.",
                    retryable=True,
                    details={"provider": "okx"},
                )
                raise PriceProviderError(
                    "OKX price provider is unavailable.", retryable=True
                ) from exc
            self.last_error = None
            if self.ttl_seconds:
                self._cache[key] = (now + self.ttl_seconds, payload)
            return payload

    def _asset_body(self, asset: Asset) -> dict[str, str]:
        return {
            "chainIndex": self._chain_index(asset),
            "tokenContractAddress": (asset.address or "").lower(),
        }

    def _history_query(self, asset, period, begin_ms, end_ms, cursor, limit):
        query = {
            "chainIndex": self._chain_index(asset),
            "tokenContractAddress": asset.address or "",
            "period": period,
            "limit": str(limit),
        }
        if begin_ms is not None:
            query["begin"] = str(begin_ms)
        if end_ms is not None:
            query["end"] = str(end_ms)
        if cursor is not None:
            query["cursor"] = cursor
        return query

    def _find_row(self, rows, asset):
        chain = self._chain_index(asset)
        address = asset.address or ""
        return next(
            (
                row
                for row in rows
                if isinstance(row, dict)
                and str(row.get("chainIndex")) == chain
                and str(row.get("tokenContractAddress") or "").lower() == address.lower()
            ),
            None,
        )

    def _chain_index(self, asset: Asset) -> str:
        try:
            return self.chain_index_by_name[asset.chain.upper()]
        except KeyError as exc:
            raise PriceProviderError(
                "Unsupported OKX chain.", code="OKX_CHAIN_UNSUPPORTED"
            ) from exc

    def _candle_address(self, asset: Asset) -> str:
        return (asset.address or "").lower()

    @staticmethod
    def _asset_key(asset):
        return asset.chain.upper(), (asset.address or "").lower(), asset.symbol.upper()

    @staticmethod
    def _rows(payload):
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise PriceProviderError("Malformed OKX price response.")
        return rows

    @staticmethod
    def _decimal(value, field):
        try:
            result = Decimal(str(value))
            if not result.is_finite() or result < 0:
                raise InvalidOperation
            return result
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PriceProviderError(f"Malformed OKX {field}.") from exc

    @classmethod
    def _optional_decimal(cls, value, field):
        return None if value in (None, "") else cls._decimal(value, field)

    @staticmethod
    def _optional_int(value):
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise PriceProviderError("Malformed holder count.") from exc

    @staticmethod
    def _timestamp(value):
        if value in (None, ""):
            return None
        try:
            number = int(str(value))
            if number > 10_000_000_000:
                number //= 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (TypeError, ValueError, OSError) as exc:
            raise PriceProviderError("Malformed OKX timestamp.") from exc
