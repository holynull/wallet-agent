from datetime import datetime, timezone
from decimal import Decimal

import pytest

from wallet_agent.domain.models import Asset
from wallet_agent.prices.okx import OkxPriceProvider, PriceProviderError

ETH = Asset(chain="ETH", symbol="ETH", decimals=18)
USDC = Asset(chain="ETH", symbol="USDC", decimals=6, address="0xAb" + "1" * 38)


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def request(self, method, path, *, query=None, body=None):
        self.calls.append((method, path, query, body))
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_current_prices_batch_normalizes_provider_and_empty_results():
    client = FakeClient(
        {
            "/api/v6/dex/market/price": {
                "code": "0",
                "data": [
                    {"chainIndex": "1", "tokenContractAddress": "", "price": "2500.25"},
                    {
                        "chainIndex": "1",
                        "tokenContractAddress": USDC.address.lower(),
                        "price": "1.00",
                        "time": "1726870923000",
                    },
                ],
            }
        }
    )
    provider = OkxPriceProvider(client, {"ETH": "1"})

    prices = await provider.get_prices([ETH, USDC])

    assert [item.usd_price for item in prices] == [Decimal("2500.25"), Decimal("1.00")]
    assert all(item.provider == "okx" for item in prices)
    assert prices[1].observed_at == datetime(2024, 9, 20, 22, 22, 3, tzinfo=timezone.utc)
    assert client.calls[0][0:2] == ("POST", "/api/v6/dex/market/price")
    assert client.calls[0][3] == [
        {"chainIndex": "1", "tokenContractAddress": ""},
        {"chainIndex": "1", "tokenContractAddress": USDC.address.lower()},
    ]


@pytest.mark.asyncio
async def test_market_details_normalizes_decimal_metrics():
    client = FakeClient(
        {
            "/api/v6/dex/market/price-info": {
                "code": "0",
                "data": [
                    {
                        "chainIndex": "1",
                        "tokenContractAddress": "",
                        "price": "2500",
                        "marketCap": "1000000.5",
                        "volume24h": "123.4",
                    }
                ],
            }
        }
    )
    provider = OkxPriceProvider(client, {"ETH": "1"})

    details = await provider.get_market_details([ETH])

    assert details[0].asset == ETH
    assert details[0].usd_price == Decimal("2500")
    assert details[0].market_cap == Decimal("1000000.5")
    assert details[0].volume_24h == Decimal("123.4")


@pytest.mark.asyncio
async def test_history_and_candles_encode_queries_and_parse_tuples():
    client = FakeClient(
        {
            "/api/v6/dex/index/historical-price": {
                "code": "0",
                "data": {
                    "history": [{"price": "2.5", "ts": "1726870923000"}],
                    "cursor": "next",
                },
            },
            "/api/v6/dex/market/candles": {
                "code": "0",
                "data": [["1726870923000", "1", "3", "0.5", "2", "4", "8", "1"]],
            },
        }
    )
    provider = OkxPriceProvider(client, {"ETH": "1"})

    history = await provider.get_historical_prices(
        USDC, period="1h", begin_ms=1000, end_ms=2000, cursor="old", limit=50
    )
    candles = await provider.get_candles(
        USDC, bar="1H", before_ms=2000, after_ms=1000, limit=100
    )

    assert history.next_cursor == "next"
    assert history.points[0].price == Decimal("2.5")
    assert history.points[0].observed_at == datetime(
        2024, 9, 20, 22, 22, 3, tzinfo=timezone.utc
    )
    assert candles[0].open == Decimal("1")
    assert candles[0].high == Decimal("3")
    assert candles[0].low == Decimal("0.5")
    assert candles[0].close == Decimal("2")
    assert candles[0].confirmed is True
    assert client.calls[0][2] == {
        "chainIndex": "1",
        "tokenContractAddress": USDC.address,
        "period": "1h",
        "begin": "1000",
        "end": "2000",
        "cursor": "old",
        "limit": "50",
    }
    assert client.calls[1][2]["tokenContractAddress"] == USDC.address.lower()


@pytest.mark.asyncio
async def test_price_provider_rejects_invalid_limits_and_malformed_responses():
    client = FakeClient({"/api/v6/dex/market/price": {"code": "0", "data": []}})
    provider = OkxPriceProvider(client, {"ETH": "1"})

    with pytest.raises(PriceProviderError):
        await provider.get_historical_prices(ETH, period="2h")
    with pytest.raises(PriceProviderError):
        await provider.get_candles(ETH, bar="1H", limit=300)

    client.responses["/api/v6/dex/market/price"] = {
        "code": "0",
        "data": [{"chainIndex": "1", "tokenContractAddress": "", "price": "NaN"}],
    }
    with pytest.raises(PriceProviderError):
        await provider.get_prices([ETH])
