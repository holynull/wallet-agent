from decimal import Decimal

import pytest
from langgraph.types import Command

from wallet_agent.domain.models import (
    Asset,
    NormalizedQuote,
    UnsignedTransaction,
)
from wallet_agent.graph.build import build_graph


class PrepareProvider:
    provider_name = "bridgers"

    def __init__(self):
        self.prepare_calls = 0

    async def prepare(self, quote):
        self.prepare_calls += 1
        return UnsignedTransaction(
            chain="BASE",
            to="0xrouter",
            data="0xdata",
            value="0x0",
            provider="bridgers",
            provider_reference=quote.provider_reference,
        )


def selected_quote():
    asset = Asset(chain="BASE", symbol="USDC", decimals=6, address="0x1")
    dest = Asset(chain="BSC", symbol="USDT", decimals=6, address="0x2")
    return NormalizedQuote(
        provider="bridgers",
        source_asset=asset,
        destination_asset=dest,
        input_amount=Decimal("1"),
        input_amount_raw="1000000",
        expected_output=Decimal("1"),
        expected_output_raw="1000000",
        provider_reference="ref",
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_prepare_interrupt_resume_calls_provider_once():
    provider = PrepareProvider()
    graph = build_graph(model=object(), providers=[provider])
    config = {"configurable": {"thread_id": "resume-1"}}
    initial = {
        "conversation_id": "c",
        "user_id": "u",
        "intent": "swap_prepare",
        "selected_quote": selected_quote(),
        "swap_request": {
            "source_asset": {"chain": "BASE", "symbol": "USDC", "decimals": 6, "address": "0x1"},
            "destination_asset": {
                "chain": "BSC",
                "symbol": "USDT",
                "decimals": 6,
                "address": "0x2",
            },
            "input_amount": "1",
            "input_amount_raw": "1000000",
            "sender_address": "0x1",
            "recipient_address": "0x2",
        },
    }
    paused = await graph.ainvoke(initial, config=config)
    assert paused["__interrupt__"]
    resumed = await graph.ainvoke(Command(resume={"approved": True}), config=config)
    assert resumed["pending_transaction"]["to"] == "0xrouter"
    assert provider.prepare_calls == 1
    await graph.ainvoke(Command(resume={"approved": True}), config=config)
    assert provider.prepare_calls == 1
