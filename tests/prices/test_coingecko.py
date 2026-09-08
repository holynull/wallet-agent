from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest

from wallet_agent.domain.models import Asset
from wallet_agent.prices.coingecko import CoinGeckoPriceProvider


class FakeJsonTransport:
    def __init__(self, response: dict[str, Any] | Exception):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"path": path, "params": params, "headers": headers})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


ETH = Asset(chain="EVM", symbol="ETH", decimals=18)
USDC = Asset(chain="EVM", symbol="USDC", decimals=6, address="0x" + "3" * 40)


@pytest.mark.asyncio
async def test_coingecko_normalizes_prices_and_uses_ttl_cache():
    transport = FakeJsonTransport(
        {
            "ethereum": {"usd": 3200.5, "last_updated_at": 1_700_000_000},
            "usd-coin": {"usd": 1.0, "last_updated_at": 1_700_000_000},
        }
    )
    provider = CoinGeckoPriceProvider(
        transport,
        token_id_by_address={USDC.address.lower(): "usd-coin"},
        native_id_by_symbol={"ETH": "ethereum"},
        ttl_seconds=60,
    )

    first = await provider.get_prices([ETH, USDC])
    second = await provider.get_prices([ETH, USDC])

    assert [item.asset.symbol for item in first] == ["ETH", "USDC"]
    assert [item.usd_price for item in first] == [Decimal("3200.5"), Decimal("1")]
    assert all(item.observed_at is not None for item in first)
    assert first[0].observed_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert len(transport.calls) == 1
    assert [item.asset.symbol for item in second] == ["ETH", "USDC"]
    assert second[0].observed_at is not None
    assert provider.last_error is None


@pytest.mark.asyncio
async def test_coingecko_returns_empty_list_and_retryable_error_when_unavailable():
    transport = FakeJsonTransport(httpx.ConnectError("coin gecko down"))
    provider = CoinGeckoPriceProvider(
        transport,
        native_id_by_symbol={"ETH": "ethereum"},
        ttl_seconds=0,
    )

    prices = await provider.get_prices([ETH])

    assert prices == []
    assert provider.last_error is not None
    assert provider.last_error.code == "COINGECKO_UNAVAILABLE"
    assert provider.last_error.retryable is True
    assert provider.last_error.details["provider"] == "coingecko"
