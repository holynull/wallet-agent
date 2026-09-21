from decimal import Decimal

import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.domain.models import (
    Asset,
    HistoricalPricePage,
    HistoricalPricePoint,
    PriceCandle,
    TokenMarketDetails,
    TokenPrice,
)


class PriceProvider:
    async def get_prices(self, assets):
        return [TokenPrice(asset=assets[0], usd_price=Decimal("2"), provider="okx")]

    async def get_market_details(self, assets):
        return [TokenMarketDetails(asset=assets[0], usd_price=Decimal("2"), holder_count=7)]

    async def get_historical_prices(self, asset, **kwargs):
        return HistoricalPricePage(
            points=[HistoricalPricePoint(price=Decimal("2"), observed_at="2024-01-01T00:00:00Z")],
            next_cursor="next",
        )

    async def get_candles(self, asset, **kwargs):
        return [
            PriceCandle(
                observed_at="2024-01-01T00:00:00Z",
                open=Decimal("1"),
                high=Decimal("2"),
                low=Decimal("1"),
                close=Decimal("2"),
                volume=Decimal("3"),
                volume_usd=Decimal("4"),
                confirmed=True,
            )
        ]


@pytest.mark.asyncio
async def test_market_history_and_candle_routes_return_typed_normalized_data():
    app = create_app(price_provider=PriceProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        market = await client.get(
            "/v1/prices/market",
            params={"chain": "ETH", "address": "0x" + "1" * 40},
        )
        history = await client.get(
            "/v1/prices/history",
            params={"chain": "ETH", "address": "0x" + "1" * 40, "period": "1h", "limit": 50},
        )
        candles = await client.get(
            "/v1/prices/candles",
            params={"chain": "ETH", "address": "0x" + "1" * 40, "bar": "1H", "limit": 100},
        )
    assert market.status_code == 200
    assert market.json()["details"][0]["holder_count"] == 7
    assert history.json()["next_cursor"] == "next"
    assert candles.json()["candles"][0]["confirmed"] is True


@pytest.mark.asyncio
async def test_price_routes_validate_provider_limits_and_disabled_mode():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        disabled = await client.get(
            "/v1/prices/history",
            params={"chain": "ETH", "address": "0x" + "1" * 40, "period": "1h"},
        )
        invalid = await client.get(
            "/v1/prices/history",
            params={"chain": "ETH", "address": "0x" + "1" * 40, "period": "2h", "limit": 201},
        )
    assert disabled.status_code == 503
    assert disabled.json()["code"] == "PRICE_PROVIDER_UNAVAILABLE"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_PRICE_QUERY"


@pytest.mark.asyncio
async def test_price_route_does_not_accept_arbitrary_okx_path_or_credentials():
    app = create_app(price_provider=PriceProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/prices/market",
            params={
                "chain": "ETH",
                "address": "0x" + "1" * 40,
                "path": "/api/v6/private",
                "api_key": "secret",
            },
        )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
