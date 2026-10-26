"""A broadcast or a fresh transfer-collection turn must drop a stale pending tx.

Regression for the App-side report where a completed BSC transfer left
``state.pending_transaction`` behind, so a follow-up "再转 0.1 usdt" react turn
rendered the old confirmation card next to the chain-selection card.
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage


class SlotFillReactModel:
    """Calls ``transfer_quick_replies`` once, then answers in prose."""

    async def invoke_react(self, messages, tools):
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "qr-1",
                        "name": "transfer_quick_replies",
                        "args": {},
                    }
                ],
            )
        return AIMessage(content="请问你想转多少 USDT？")


def _stale_bnb_pending() -> dict:
    return {
        "chain": "BSC",
        "chain_id": 56,
        "to": "0x" + "2" * 40,
        "data": "0x",
        "value": "100000000000000",
        "display": {"action": "native_transfer", "symbol": "BNB"},
    }


@pytest.mark.asyncio
async def test_react_turn_drops_stale_pending_after_broadcast():
    from wallet_agent.graph.build import build_graph

    model = SlotFillReactModel()
    graph = build_graph(model=model)
    result = await graph.ainvoke(
        {
            "request": {"message": "再转 0.1 usdt"},
            "pending_transaction": _stale_bnb_pending(),
            "preflight": {"ok": True, "checks": []},
            "broadcast_tx_hash": "0x0e99eb12" + "0" * 56,
            "broadcast_status": "confirmed",
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "react-drops-stale-pending"}},
    )

    assert result["response"]["kind"] == "react"
    assert result.get("pending_transaction") is None
    assert result.get("preflight") is None


class BalanceQueryModel:
    """Answers a balance query with ``wallet_balance`` and no transfer tools."""

    async def invoke_react(self, messages, tools):
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if not tool_messages:
            from tests.graph.test_unified_react import WALLET

            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "bal-1",
                        "name": "wallet_balance",
                        "args": {"chain": "BSC", "address": WALLET},
                    }
                ],
            )
        return AIMessage(content="你的 BSC 余额约 0.5 BNB，约 384 美元。")


@pytest.mark.asyncio
async def test_react_balance_turn_drops_stale_pending_without_broadcast():
    """A balance query while an unsigned transfer is still pending must drop
    the stale pending_transaction + preflight.

    Regression for the App-side report: after a prepared transfer, asking
    "余额" made the App render the transfer confirmation card again next to
    the balance answer. The card only survives while the transfer is the
    subject of the conversation; a balance turn is not, so it must be revoked.
    """
    from tests.graph.test_unified_react import WALLET, WalletProvider
    from wallet_agent.graph.build import build_graph

    model = BalanceQueryModel()
    graph = build_graph(model=model, wallet_provider=WalletProvider())
    result = await graph.ainvoke(
        {
            "request": {"message": "余额"},
            "wallet_context": {"address": WALLET, "chain": "BSC"},
            "pending_transaction": _stale_bnb_pending(),
            "preflight": {"ok": True, "checks": []},
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "react-balance-drops-stale-pending"}},
    )

    assert result["response"]["kind"] == "react"
    # The balance answer is projected as the portfolio card...
    assert isinstance(result["response"].get("portfolio"), dict)
    # ...and the stale unsigned transfer is dropped, so the App must not
    # re-render the transfer confirmation card next to it.
    assert result.get("pending_transaction") is None
    assert result.get("preflight") is None


@pytest.mark.asyncio
async def test_react_transfer_slot_fill_drops_stale_pending_without_broadcast():
    """A new transfer-collection turn replaces an old, still-unsigned prepare."""
    from wallet_agent.graph.build import build_graph

    model = SlotFillReactModel()
    graph = build_graph(model=model)
    result = await graph.ainvoke(
        {
            "request": {"message": "再转 0.1 usdt"},
            "pending_transaction": _stale_bnb_pending(),
            "preflight": {"ok": True, "checks": []},
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "react-slotfill-drops-stale-pending"}},
    )

    assert result["response"]["kind"] == "react"
    assert result.get("pending_transaction") is None
    assert result.get("preflight") is None
