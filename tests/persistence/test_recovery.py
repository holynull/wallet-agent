from decimal import Decimal

import pytest
from langgraph.types import Command

from wallet_agent.domain.models import Asset, NormalizedQuote, UnsignedTransaction
from wallet_agent.graph.build import build_graph
from wallet_agent.persistence import LazyAsyncPostgresSaver, create_checkpointer


class StrictModel:
    async def invoke_react(self, _messages, _tools):
        raise AssertionError("lifecycle resume must not invoke the ReAct model")


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
    return NormalizedQuote(
        provider="bridgers",
        source_asset=Asset(chain="BASE", symbol="USDC", decimals=6, address="0x1"),
        destination_asset=Asset(chain="BSC", symbol="USDT", decimals=6, address="0x2"),
        input_amount=Decimal("1"),
        input_amount_raw="1000000",
        expected_output=Decimal("1"),
        expected_output_raw="1000000",
        provider_reference="ref",
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_sqlite_checkpoint_survives_graph_rebuild_without_duplicate_prepare(tmp_path):
    db_path = tmp_path / "checkpoints.db"
    first_handle = create_checkpointer(f"sqlite:///{db_path}")
    provider = PrepareProvider()
    config = {"configurable": {"thread_id": "recovery-thread"}}
    initial = {
        "conversation_id": "c",
        "user_id": "u",
        "intent": "swap_prepare",
        "swap_request": {
            "source_asset": {"chain": "BASE", "symbol": "USDC", "decimals": 6, "address": "0x1"},
            "destination_asset": {
                "chain": "BSC", "symbol": "USDT", "decimals": 6, "address": "0x2"
            },
            "input_amount": "1",
            "input_amount_raw": "1000000",
            "sender_address": "0x1",
            "recipient_address": "0x2",
        },
        "selected_quote": selected_quote(),
    }
    try:
        graph = build_graph(
            model=object(), providers=[provider], checkpointer=first_handle.checkpointer
        )
        paused = await graph.ainvoke(initial, config=config)
        assert paused["__interrupt__"]
    finally:
        await first_handle.aclose()

    second_handle = create_checkpointer(f"sqlite:///{db_path}")
    try:
        rebuilt = build_graph(
            model=object(), providers=[provider], checkpointer=second_handle.checkpointer
        )
        resumed = await rebuilt.ainvoke(Command(resume={"approved": True}), config=config)
        assert resumed["pending_transaction"]["to"] == "0xrouter"
        assert provider.prepare_calls == 1
    finally:
        await second_handle.aclose()


@pytest.mark.asyncio
async def test_split_sqlite_graph_resumes_lifecycle_interrupt_after_rebuild(tmp_path):
    db_path = tmp_path / "split-checkpoints.db"
    config = {"configurable": {"thread_id": "split-recovery-thread"}}
    initial = {
        "conversation_id": "c",
        "user_id": "u",
        "forced_intent": "swap_prepare",
        "intent": "swap_prepare",
        "swap_request": {
            "source_asset": {"chain": "BASE", "symbol": "USDC", "decimals": 6, "address": "0x1"},
            "destination_asset": {
                "chain": "BSC", "symbol": "USDT", "decimals": 6, "address": "0x2"
            },
            "input_amount": "1",
            "input_amount_raw": "1000000",
            "sender_address": "0x1",
            "recipient_address": "0x2",
        },
        "selected_quote": selected_quote(),
    }
    provider = PrepareProvider()
    first_handle = create_checkpointer(f"sqlite:///{db_path}")
    try:
        graph = build_graph(
            model=StrictModel(),
            providers=[provider],
            checkpointer=first_handle.checkpointer,
            split_graphs=True,
            require_react=True,
        )
        paused = await graph.ainvoke(initial, config=config)
        assert paused["__interrupt__"]
    finally:
        await first_handle.aclose()

    second_handle = create_checkpointer(f"sqlite:///{db_path}")
    try:
        rebuilt = build_graph(
            model=StrictModel(),
            providers=[provider],
            checkpointer=second_handle.checkpointer,
            split_graphs=True,
            require_react=True,
        )
        resumed = await rebuilt.ainvoke(Command(resume={"approved": True}), config=config)
    finally:
        await second_handle.aclose()

    assert resumed["pending_transaction"]["to"] == "0xrouter"
    assert provider.prepare_calls == 1


def test_postgres_checkpointer_is_selected_without_eager_connection():
    handle = create_checkpointer(
        "postgresql+psycopg://wallet_agent:secret@postgres:5432/wallet_agent"
    )

    assert isinstance(handle.checkpointer, LazyAsyncPostgresSaver)
    assert handle.checkpointer.dsn == (
        "postgresql://wallet_agent:secret@postgres:5432/wallet_agent"
    )
