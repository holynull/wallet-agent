from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from langgraph.types import Command

from wallet_agent.domain.models import (
    Asset,
    NormalizedQuote,
    UnsignedTransaction,
)
from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import confirmation_payload_hash


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


class ReparseModel:
    def __init__(self):
        self.classify_calls = 0
        self.extract_calls = 0

    async def classify(self, _request):
        self.classify_calls += 1
        return {"intent": "swap_quote"}

    async def extract(self, _kind, _request):
        self.extract_calls += 1
        return {
            "source_chain": "BASE",
            "destination_chain": "BASE",
            "source_symbol": "USDC",
            "destination_symbol": "USDT",
            "source_token_address": "0x1",
            "destination_token_address": "0x2",
            "source_decimals": 6,
            "destination_decimals": 6,
            "input_amount": "1",
        }


class ReparseProvider:
    provider_name = "bridgers"

    async def quote(self, request):
        return NormalizedQuote(
            provider="bridgers",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal("1"),
            expected_output_raw="1000000",
            provider_reference="reparsed",
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


def test_confirmation_payload_hash_is_stable_across_key_order():
    assert confirmation_payload_hash({"provider": "bridgers", "input_amount": "1"}) == (
        confirmation_payload_hash({"input_amount": "1", "provider": "bridgers"})
    )


@pytest.mark.asyncio
async def test_stale_task_revision_is_rejected_before_prepare():
    provider = PrepareProvider()
    graph = build_graph(model=object(), providers=[provider])
    quote = selected_quote()
    summary = {
        "provider": quote["provider"],
        "provider_reference": quote["provider_reference"],
        "source_asset": quote["source_asset"],
        "destination_asset": quote["destination_asset"],
        "input_amount": quote["input_amount"],
        "expected_output": quote["expected_output"],
    }
    now = datetime.now(timezone.utc)
    result = await graph.ainvoke(
        {
            "conversation_id": "stale-confirmation",
            "intent": "swap_prepare",
            "active_task": {
                "task_id": "swap-task",
                "kind": "swap",
                "status": "ready",
                "stage": "ready_for_prepare",
                "revision": 2,
                "slots": {},
                "slot_sources": {},
                "missing_fields": [],
            },
            "selected_quote": quote,
            "wallet_context": {"address": "0x1", "chain": "BASE"},
            "swap_request": {
                "source_asset": quote["source_asset"],
                "destination_asset": quote["destination_asset"],
                "input_amount": "1",
                "input_amount_raw": "1000000",
                "sender_address": "0x1",
                "recipient_address": "0x2",
            },
            "confirmation_state": {
                "action": "swap",
                "status": "requested",
                "requested_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "summary": summary,
                "reason": None,
                "task_id": "swap-task",
                "task_revision": 1,
                "payload_hash": confirmation_payload_hash(summary),
            },
        },
        config={"configurable": {"thread_id": "stale-confirmation"}},
    )

    assert result["response"]["errors"][0]["code"] == "CONFIRMATION_STALE"
    assert provider.prepare_calls == 0


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


@pytest.mark.asyncio
async def test_new_message_during_confirmation_reparses_and_invalidates_old_confirmation():
    model = ReparseModel()
    graph = build_graph(model=model, providers=[ReparseProvider()])
    quote = selected_quote()
    config = {"configurable": {"thread_id": "reparse-after-confirmation"}}

    paused = await graph.ainvoke(
        {
            "conversation_id": "reparse-after-confirmation",
            "intent": "swap_prepare",
            "forced_intent": "swap_prepare",
            "selected_quote": quote,
            "swap_request": {
                "source_asset": quote["source_asset"],
                "destination_asset": quote["destination_asset"],
                "input_amount": "1",
                "input_amount_raw": "1000000",
                "sender_address": "0x1",
                "recipient_address": "0x2",
            },
        },
        config=config,
    )
    assert paused["__interrupt__"]

    result = await graph.ainvoke(
        Command(
            resume={
                "request": {"message": "改成 1 USDC 换 USDT"},
                "wallet_context": {"address": "0x1", "chain": "BASE"},
            }
        ),
        config=config,
    )

    assert model.classify_calls == 1
    assert model.extract_calls == 1
    assert result["response"]["kind"] == "swap_quote"
    assert result["swap_request"]["source_asset"]["symbol"] == "USDC"
    assert result["confirmation_state"] is None
