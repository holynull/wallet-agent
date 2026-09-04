from decimal import Decimal

import pytest

from wallet_agent.domain.models import Asset, NormalizedQuote, SwapQuoteRequest, UnsignedTransaction
from wallet_agent.graph.build import build_graph


class FakeModel:
    async def ainvoke(self, value):
        return {"intent": value.get("intent", "clarification")}


class FakeProvider:
    def __init__(self, name: str):
        self.provider_name = name
        self.calls = []

    async def quote(self, request):
        self.calls.append(("quote", request))
        return NormalizedQuote(
            provider=self.provider_name,
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal("9"),
            expected_output_raw="9000000",
            provider_reference=f"{self.provider_name}-ref",
        )

    async def prepare(self, quote):
        self.calls.append(("prepare", quote))
        return UnsignedTransaction(
            chain=quote.source_asset.chain,
            to="0xrouter",
            data="0xdata",
            value="0x0",
            provider=self.provider_name,
            provider_reference=quote.provider_reference,
        )


def quote_request():
    return SwapQuoteRequest(
        source_asset=Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x1"),
        destination_asset=Asset(chain="BSC", chain_id=56, symbol="USDT", decimals=6, address="0x2"),
        input_amount=Decimal("10"),
        input_amount_raw="10000000",
        sender_address="0xsender",
        recipient_address="0xrecipient",
    )


@pytest.mark.asyncio
async def test_quote_path_fans_out_and_reduces_candidates():
    graph = build_graph(
        model=FakeModel(), providers=[FakeProvider("bridgers"), FakeProvider("omnibridge")]
    )
    result = await graph.ainvoke(
        {
            "conversation_id": "c1",
            "user_id": "u1",
            "intent": "swap_quote",
            "swap_request": quote_request(),
        },
        config={"configurable": {"thread_id": "t-1"}},
    )
    assert {quote["provider"] for quote in result["quote_candidates"]} == {"bridgers", "omnibridge"}


@pytest.mark.asyncio
async def test_malformed_model_output_routes_to_clarification():
    class BadModel:
        async def ainvoke(self, value):
            return "not structured"

    graph = build_graph(model=BadModel(), providers=[])
    result = await graph.ainvoke(
        {"conversation_id": "c1", "user_id": "u1", "message": "swap"},
        config={"configurable": {"thread_id": "bad"}},
    )
    assert result["intent"] == "clarification"
    assert result["response"]["kind"] == "clarification"
