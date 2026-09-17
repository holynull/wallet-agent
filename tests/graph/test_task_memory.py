from decimal import Decimal

import pytest

from wallet_agent.domain.models import (
    Asset,
    AssetQuery,
    FeeEstimate,
    NormalizedQuote,
    TokenBalance,
    UnsignedTransaction,
)
from wallet_agent.graph import build_graph
from wallet_agent.models import RouteDecision, SwapSlotPatch, TransferSlotPatch
from wallet_agent.persistence import create_checkpointer

WALLET = "0x" + "1" * 40
RECIPIENT = "0x" + "2" * 40
USDC = "0x" + "3" * 40
USDT = "0x" + "4" * 40


class UnderstandingModel:
    def __init__(self, classifications, *, transfer=(), swap=()):
        self.classifications = iter(classifications)
        self.patches = {"transfer": iter(transfer), "swap": iter(swap)}
        self.extracted_kinds = []

    async def classify(self, _request):
        return RouteDecision(intent=next(self.classifications))

    async def extract(self, task_kind, _request):
        self.extracted_kinds.append(task_kind)
        return next(self.patches[task_kind])


class Provider:
    provider_name = "bridgers"

    def __init__(self):
        self.quote_calls = 0

    async def list_assets(self, query: AssetQuery):
        assets = {
            "USDC": Asset(
                chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address=USDC
            ),
            "USDT": Asset(
                chain="BASE", chain_id=8453, symbol="USDT", decimals=6, address=USDT
            ),
        }
        asset = assets.get(str(query.search).upper())
        return [asset] if asset and str(query.chain).upper() == "BASE" else []

    async def quote(self, request):
        self.quote_calls += 1
        return NormalizedQuote(
            provider="bridgers",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=request.input_amount,
            expected_output_raw=request.input_amount_raw,
            provider_reference=f"quote-{self.quote_calls}",
        )


class Chain:
    async def validate_address(self, address):
        return address.startswith("0x") and len(address) == 42

    async def get_native_balance(self, _address):
        return TokenBalance(
            asset=Asset(chain="BASE", chain_id=8453, symbol="ETH", decimals=18),
            amount=Decimal("2"),
            amount_raw="2000000000000000000",
        )

    async def get_token_balances(self, _address):
        return []

    async def estimate_fee(self, *, to=None, data=None):
        del to, data
        return FeeEstimate(
            chain="BASE",
            chain_id=8453,
            asset=Asset(chain="BASE", chain_id=8453, symbol="ETH", decimals=18),
            amount=Decimal("0.0001"),
            amount_raw="100000000000000",
        )

    def build_native_transfer(self, *, from_address, to_address, amount_raw):
        del from_address
        return UnsignedTransaction(
            chain="BASE", chain_id=8453, to=to_address, data="0x", value=amount_raw
        )


def wallet_context():
    return {"address": WALLET, "chain": "BASE", "chain_id": 8453, "native_symbol": "ETH"}


def turn(message):
    return {
        "conversation_id": "task-memory",
        "user_id": "eval-user",
        "request": {"message": message},
        "wallet_context": wallet_context(),
    }


@pytest.mark.asyncio
async def test_transfer_clarification_retains_known_slots_in_active_task():
    model = UnderstandingModel(
        ["transfer"],
        transfer=[TransferSlotPatch(chain="BASE", symbol="ETH", amount="0.01")],
    )
    graph = build_graph(model=model, chains={"BASE": Chain()})

    result = await graph.ainvoke(
        turn("从当前 Base 钱包转 0.01 ETH"),
        config={"configurable": {"thread_id": "task-transfer"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert result["response"]["missing_fields"] == ["transfer_recipient"]
    assert result["active_task"]["kind"] == "transfer"
    assert result["active_task"]["slots"] == {
        "chain": "BASE",
        "symbol": "ETH",
        "amount": "0.01",
    }
    assert result["transfer_draft"]["transfer_amount"] == "0.01"


@pytest.mark.asyncio
async def test_swap_follow_up_classified_as_clarification_still_merges_chain_patch():
    model = UnderstandingModel(
        ["swap_quote", "clarification"],
        swap=[
            SwapSlotPatch(source_symbol="USDC", destination_symbol="USDT", input_amount="1"),
            SwapSlotPatch(source_chain="BASE", destination_chain="BASE"),
        ],
    )
    provider = Provider()
    graph = build_graph(model=model, providers=[provider])
    config = {"configurable": {"thread_id": "task-swap-followup"}}

    first = await graph.ainvoke(turn("用 1 USDC 换 USDT"), config=config)
    second = await graph.ainvoke(turn("都在 Base 链"), config=config)

    assert first["response"]["kind"] == "clarification"
    assert second["response"]["kind"] == "swap_quote"
    assert second["active_task"]["kind"] == "swap"
    assert second["active_task"]["revision"] == 2
    assert second["active_task"]["slots"]["source_symbol"] == "USDC"
    assert provider.quote_calls == 1
    assert model.extracted_kinds == ["swap", "swap"]


@pytest.mark.asyncio
async def test_transient_balance_query_preserves_active_swap_task():
    model = UnderstandingModel(
        ["swap_quote", "wallet_query"],
        swap=[SwapSlotPatch(source_symbol="USDC", destination_symbol="USDT", input_amount="1")],
    )
    graph = build_graph(model=model, providers=[Provider()], chains={"BASE": Chain()})
    config = {"configurable": {"thread_id": "task-transient-balance"}}

    first = await graph.ainvoke(turn("用 1 USDC 换 USDT"), config=config)
    second = await graph.ainvoke(turn("先看看我的余额"), config=config)

    assert second["response"]["kind"] == "wallet_query"
    assert second["active_task"] == first["active_task"]
    assert second["active_task"]["status"] == "collecting"


@pytest.mark.asyncio
async def test_swap_correction_replaces_previous_quote():
    model = UnderstandingModel(
        ["swap_quote", "swap_quote"],
        swap=[
            SwapSlotPatch(
                source_chain="BASE",
                destination_chain="BASE",
                source_symbol="USDC",
                destination_symbol="USDT",
                input_amount="1",
            ),
            SwapSlotPatch(input_amount="2"),
        ],
    )
    provider = Provider()
    graph = build_graph(model=model, providers=[provider])
    config = {"configurable": {"thread_id": "task-swap-correction"}}

    first = await graph.ainvoke(turn("在 Base 用 1 USDC 换 USDT"), config=config)
    second = await graph.ainvoke(turn("改成 2 USDC"), config=config)

    assert first["response"]["quotes"][0]["provider_reference"] == "quote-1"
    assert [item["provider_reference"] for item in second["response"]["quotes"]] == ["quote-2"]
    assert second["active_task"]["revision"] == 2
    assert second["swap_request"]["input_amount"] == "2"


@pytest.mark.asyncio
async def test_cancel_marks_active_task_cancelled_and_is_idempotent():
    model = UnderstandingModel(
        ["swap_quote"],
        swap=[SwapSlotPatch(source_symbol="USDC", destination_symbol="USDT", input_amount="1")],
    )
    graph = build_graph(model=model, providers=[Provider()])
    config = {"configurable": {"thread_id": "task-cancel"}}
    await graph.ainvoke(turn("用 1 USDC 换 USDT"), config=config)

    first = await graph.ainvoke(turn("取消兑换"), config=config)
    second = await graph.ainvoke(turn("取消兑换"), config=config)

    assert first["response"]["kind"] == "cancelled"
    assert second["response"]["kind"] == "cancelled"
    assert second["active_task"]["status"] == "cancelled"
    assert second["active_task"]["revision"] == first["active_task"]["revision"]


@pytest.mark.asyncio
async def test_sqlite_restart_after_clarification_accepts_next_slot_patch(tmp_path):
    db_path = tmp_path / "task-memory.db"
    config = {"configurable": {"thread_id": "task-restart"}}
    first_handle = create_checkpointer(f"sqlite:///{db_path}")
    try:
        first_graph = build_graph(
            model=UnderstandingModel(
                ["transfer"],
                transfer=[TransferSlotPatch(chain="BASE", symbol="ETH", amount="0.01")],
            ),
            chains={"BASE": Chain()},
            checkpointer=first_handle.checkpointer,
        )
        first = await first_graph.ainvoke(turn("转 0.01 ETH"), config=config)
        assert first["response"]["missing_fields"] == ["transfer_recipient"]
    finally:
        await first_handle.aclose()

    second_handle = create_checkpointer(f"sqlite:///{db_path}")
    try:
        second_graph = build_graph(
            model=UnderstandingModel(
                ["clarification"],
                transfer=[TransferSlotPatch(recipient=RECIPIENT)],
            ),
            chains={"BASE": Chain()},
            checkpointer=second_handle.checkpointer,
        )
        second = await second_graph.ainvoke(turn(f"收款地址是 {RECIPIENT}"), config=config)

        assert second["response"]["kind"] == "transfer_prepare"
        assert second["pending_transaction"]["to"] == RECIPIENT
        assert second["active_task"]["revision"] == 2
    finally:
        await second_handle.aclose()
