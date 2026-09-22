from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wallet_agent.domain.models import Asset, AssetQuery, NormalizedQuote, SwapQuoteRequest
from wallet_agent.providers.bridgers import BridgersProvider
from wallet_agent.providers.http import ProviderResponseError
from wallet_agent.providers.omnibridge import OmniBridgeProvider

FIXTURES = Path(__file__).parent / "fixtures" / "providers"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


class FakeTransport:
    def __init__(self, *responses: dict[str, Any]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post(
        self, path: str, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"path": path, "payload": payload, "idempotency_key": idempotency_key})
        return self.responses.pop(0)


def bridgers_request() -> SwapQuoteRequest:
    return SwapQuoteRequest(
        source_asset=Asset(
            chain="BASE",
            chain_id=8453,
            symbol="USDC",
            decimals=6,
            address="0x0000000000000000000000000000000000000011",
        ),
        destination_asset=Asset(
            chain="BSC",
            chain_id=56,
            symbol="USDT",
            decimals=6,
            address="0x0000000000000000000000000000000000000022",
        ),
        input_amount=Decimal("10"),
        input_amount_raw="10000000",
        sender_address="0x1234567890abcdef1234567890abcdef12345678",
        recipient_address="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
    )


def omni_request() -> SwapQuoteRequest:
    return SwapQuoteRequest(
        source_asset=Asset(chain="ETH", chain_id=1, symbol="ETH", decimals=18),
        destination_asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
        input_amount=Decimal("1.5"),
        input_amount_raw="1500000000000000000",
        sender_address="0x1234567890abcdef1234567890abcdef12345678",
        recipient_address="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        refund_address="0x1234567890abcdef1234567890abcdef12345678",
        slippage_bps=200,
    )


@pytest.mark.asyncio
async def test_bridgers_fixture_quote_preserves_human_and_raw_amounts():
    quote = await BridgersProvider.from_transport(
        FakeTransport(fixture("bridgers_quote.json")), source_flag="wallet-agent"
    ).quote(bridgers_request())
    assert isinstance(quote, NormalizedQuote)
    assert quote.provider == "bridgers"
    assert quote.input_amount_raw == "10000000"
    assert quote.expected_output == Decimal("9.8")
    assert quote.expected_output_raw == "9800000"
    assert quote.minimum_output_raw == "9700000"


@pytest.mark.asyncio
async def test_bridgers_missing_tx_data_is_malformed_error():
    response = fixture("bridgers_quote.json")
    response["data"] = {}
    with pytest.raises(ProviderResponseError, match="txData"):
        await BridgersProvider.from_transport(FakeTransport(response)).quote(bridgers_request())


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["412", "999"])
async def test_bridgers_application_codes_are_structured_errors(code: str):
    response = fixture("bridgers_error.json")
    response["resCode"] = code
    with pytest.raises(ProviderResponseError) as caught:
        await BridgersProvider.from_transport(FakeTransport(response)).quote(bridgers_request())
    assert caught.value.code == code
    assert caught.value.retryable is (code == "412")


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [[], None])
async def test_bridgers_empty_or_null_catalog_is_empty(data: Any):
    response = fixture("bridgers_empty.json")
    response["data"]["tokens"] = data
    assert (
        await BridgersProvider.from_transport(FakeTransport(response)).list_assets(AssetQuery())
        == []
    )


@pytest.mark.asyncio
async def test_omnibridge_fixture_quote_exposes_provider_metadata_and_raw_amounts():
    quote = await OmniBridgeProvider.from_transport(
        FakeTransport(fixture("omnibridge_quote.json")),
        source_flag="wallet-agent",
        source_type="IOS",
    ).quote(omni_request())
    assert isinstance(quote, NormalizedQuote)
    assert quote.provider == "omnibridge"
    assert quote.input_amount_raw == "1500000000000000000"
    assert quote.expected_output_raw == "10292036633431292000"
    assert quote.provider_payload["source_flag"] == "wallet-agent"
    assert quote.provider_payload["source_type"] == "IOS"
    assert quote.provider_payload["deposit_min"] == "0.038603"
    assert quote.provider_payload["deposit_max"] == "14"


@pytest.mark.asyncio
async def test_omnibridge_quote_creates_order_from_fixture():
    order_response = {
        "resCode": 800,
        "resMsg": "Success",
        "data": {
            "orderId": "order-1",
            "platformAddr": "0x0000000000000000000000000000000000000099",
        },
    }
    transport = FakeTransport(fixture("omnibridge_quote.json"), order_response)
    provider = OmniBridgeProvider.from_transport(transport, source_flag="wallet-agent")
    order = await provider.prepare(await provider.quote(omni_request()))
    assert order.provider == "omnibridge"
    assert order.provider_order_id == "order-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [[], None])
async def test_omnibridge_empty_or_null_catalog_is_empty(data: Any):
    response = fixture("omnibridge_empty.json")
    response["data"] = data
    assert (
        await OmniBridgeProvider.from_transport(FakeTransport(response)).list_assets(AssetQuery())
        == []
    )


@pytest.mark.asyncio
async def test_omnibridge_non_800_is_provider_error():
    with pytest.raises(ProviderResponseError) as caught:
        await OmniBridgeProvider.from_transport(
            FakeTransport(fixture("omnibridge_error.json"))
        ).quote(omni_request())
    assert caught.value.code == "801"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("instantRate", "NaN"), ("instantRate", "-1"), ("depositCoinFeeRate", "Infinity")],
)
async def test_omnibridge_rejects_invalid_rate_or_fee(field: str, value: str):
    response = fixture("omnibridge_quote.json")
    response["data"][field] = value
    with pytest.raises(ProviderResponseError):
        await OmniBridgeProvider.from_transport(FakeTransport(response)).quote(omni_request())


@pytest.mark.asyncio
async def test_failed_bridgers_does_not_erase_successful_omnibridge_candidate():
    request = omni_request()
    candidates = []
    try:
        candidates.append(
            await BridgersProvider.from_transport(
                FakeTransport(fixture("bridgers_error.json"))
            ).quote(request)
        )
    except ProviderResponseError:
        pass
    candidates.append(
        await OmniBridgeProvider.from_transport(
            FakeTransport(fixture("omnibridge_quote.json"))
        ).quote(request)
    )
    assert [candidate.provider for candidate in candidates] == ["omnibridge"]
