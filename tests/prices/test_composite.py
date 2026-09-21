from decimal import Decimal

import pytest

from wallet_agent.domain.models import AgentError, Asset, TokenPrice
from wallet_agent.prices.composite import CompositePriceProvider

ETH = Asset(chain="ETH", symbol="ETH", decimals=18)
USDC = Asset(chain="ETH", symbol="USDC", decimals=6, address="0x" + "1" * 40)


class StubProvider:
    def __init__(self, prices=None, error=None):
        self.prices = prices or []
        self.error = error
        self.last_error = None
        self.calls = []

    async def get_prices(self, assets):
        self.calls.append(assets)
        if self.error:
            self.last_error = AgentError(
                code="OKX_UNAVAILABLE", message="provider unavailable", retryable=True
            )
            raise self.error
        return [price for price in self.prices if price.asset in assets]


@pytest.mark.asyncio
async def test_composite_fills_missing_assets_without_averaging_prices():
    primary = StubProvider(
        [TokenPrice(asset=ETH, usd_price="2000", provider="okx")]
    )
    fallback = StubProvider(
        [
            TokenPrice(asset=ETH, usd_price="2100", provider="coingecko"),
            TokenPrice(asset=USDC, usd_price="1", provider="coingecko"),
        ]
    )

    prices = await CompositePriceProvider(primary, fallback).get_prices([ETH, USDC])

    assert [(item.asset.symbol, item.usd_price, item.provider) for item in prices] == [
        ("ETH", Decimal("2000"), "okx"),
        ("USDC", Decimal("1"), "coingecko"),
    ]
    assert fallback.calls == [[USDC]]


@pytest.mark.asyncio
async def test_composite_keeps_fallback_prices_when_primary_is_unavailable():
    primary = StubProvider(error=RuntimeError("down"))
    fallback = StubProvider(
        [TokenPrice(asset=ETH, usd_price="2100", provider="coingecko")]
    )

    prices = await CompositePriceProvider(primary, fallback).get_prices([ETH])

    assert prices[0].provider == "coingecko"
    assert prices[0].usd_price == Decimal("2100")
