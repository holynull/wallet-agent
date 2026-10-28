"""A swap amount step must reach the App as a top-level ``open_amount_keypad``.

Regression for the App-side report: after tapping a recommended pair (e.g.
"把 ETH 换成 USDT"), the model called ``request_client_action`` with
``open_amount_keypad``, but the transfer-only reconciliation in
``finalize_response`` misread it as a transfer keypad with no user-chosen coin
and dropped the top-level ``client_action``. The App therefore never projected
the swap amount keyboard (Figma 33715:134548).

The swap keypad must be kept at the response top level and carry
``slots.intent = "swap"`` plus the tapped source asset's token metadata.
"""

from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.domain.models import Asset, TokenBalance
from wallet_agent.graph.build import build_graph


def _render_decision(**overrides):
    decision = {
        "intent": "swap",
        "source_symbol": None,
        "destination_symbol": None,
        "recommend_swap_pairs": False,
        "reason": "test",
    }
    decision.update(overrides)
    return decision


class SwapKeypadModel:
    """Calls ``request_client_action(open_amount_keypad)`` then answers in prose."""

    def __init__(self, decision=None):
        self.decision = decision or _render_decision(
            source_symbol="ETH", destination_symbol="USDT"
        )

    async def invoke_react(self, messages, tools):
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "ca-1",
                        "name": "request_client_action",
                        "args": {
                            "action": "open_amount_keypad",
                            "chain": "ETH",
                            "symbol": "ETH",
                            "reason": "请输入要兑换的 ETH 数量",
                        },
                    }
                ],
            )
        return AIMessage(content="请输入要兑换的 ETH 数量。")

    async def decide_render(self, request):
        return dict(self.decision)


def _swap_request(metadata=None, message="把 ETH 换成 USDT"):
    request = {"message": message}
    if metadata is not None:
        request["metadata"] = metadata
    return {"request": request, "conversation_history": []}


@pytest.mark.asyncio
async def test_swap_pair_keypad_reaches_top_level_client_action():
    """Tapping a recommended pair keeps the keypad with intent/source metadata."""
    graph = build_graph(model=SwapKeypadModel())
    result = await graph.ainvoke(
        _swap_request(
            metadata={
                "suggestion_data": {
                    "intent": "swap",
                    "source": {
                        "chain": "ETH",
                        "symbol": "ETH",
                        "address": "0x" + "a" * 40,
                        "decimals": 18,
                    },
                    "destination": {"chain": "ETH", "symbol": "USDT"},
                }
            }
        ),
        config={"configurable": {"thread_id": "react-swap-keypad-pair"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    client_action = response.get("client_action")
    assert client_action is not None, "swap amount keypad must be top-level"
    assert client_action["type"] == "open_amount_keypad"
    slots = client_action["slots"]
    assert slots["intent"] == "swap"
    assert slots["chain"] == "ETH"
    assert slots["symbol"] == "ETH"
    assert slots["token_address"] == "0x" + "a" * 40
    assert slots["decimals"] == 18


@pytest.mark.asyncio
async def test_typed_swap_pair_keypad_kept_with_swap_intent():
    """A typed pair ("把 ETH 换成 USDT") keeps the swap keypad."""
    graph = build_graph(model=SwapKeypadModel())
    result = await graph.ainvoke(
        _swap_request(),
        config={"configurable": {"thread_id": "react-swap-keypad-typed"}},
    )

    client_action = result["response"].get("client_action")
    assert client_action is not None
    assert client_action["type"] == "open_amount_keypad"
    assert client_action["slots"]["intent"] == "swap"
    assert client_action["slots"]["chain"] == "ETH"
    assert client_action["slots"]["symbol"] == "ETH"


@pytest.mark.asyncio
async def test_dui_pair_keypad_kept_top_level():
    """A pair message without the swap tokens still keeps the top-level keypad.

    App-side report: "USDT 兑 ETH" / "用 USDT 换 ETH" carry no "兑换/换成" token,
    so the swap intent check used to miss them and the keypad was dropped from
    the response top level (it only lingered inside ``react_results``).
    """

    class DuiKeypadModel:
        def __init__(self, decision=None):
            self.decision = decision or _render_decision(
                source_symbol="USDT", destination_symbol="ETH"
            )

        async def invoke_react(self, messages, tools):
            tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "ca-1",
                            "name": "request_client_action",
                            "args": {
                                "action": "open_amount_keypad",
                                "chain": "ETH",
                                "symbol": "USDT",
                                "reason": "请输入要兑换的 USDT 数量",
                            },
                        }
                    ],
                )
            return AIMessage(content="请输入要兑换的 USDT 数量。")

        async def decide_render(self, request):
            return dict(self.decision)

    graph = build_graph(model=DuiKeypadModel())
    for message in ("USDT 兑 ETH", "用 USDT 换 ETH"):
        result = await graph.ainvoke(
            {"request": {"message": message}, "conversation_history": []},
            config={"configurable": {"thread_id": f"react-swap-dui-{message[:6]}"}},
        )
        client_action = result["response"].get("client_action")
        assert client_action is not None, f"swap keypad must be top-level for {message!r}"
        assert client_action["type"] == "open_amount_keypad"
        slots = client_action["slots"]
        assert slots["intent"] == "swap"
        assert slots["chain"] == "ETH"
        assert slots["symbol"] == "USDT"


@pytest.mark.asyncio
async def test_typed_swap_keypad_fetches_source_balance_and_identity():
    """The keypad turn carries the exact source balance and token identity."""

    class UsdtWalletProvider:
        chain_index_by_name = {"ETH": "1"}

        async def get_token_balances(self, address, indexes):
            assert address == "0x" + "1" * 40
            assert indexes == ["1"]
            return [
                TokenBalance(
                    asset=Asset(
                        chain="ETH",
                        chain_id=1,
                        symbol="USDT",
                        decimals=6,
                        address="0xdac17f958d2ee523a2206206994597c13d831ec7",
                    ),
                    amount=Decimal("25.5"),
                    amount_raw="25500000",
                    usd_value=Decimal("25.5"),
                )
            ]

    class TypedUsdtKeypadModel:
        async def invoke_react(self, messages, tools):
            tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "ca-1",
                            "name": "request_client_action",
                            "args": {
                                "action": "open_amount_keypad",
                                "chain": "ETH",
                                "symbol": "USDT",
                                "reason": "请输入要兑换的 USDT 数量",
                            },
                        }
                    ],
                )
            return AIMessage(content="请输入要兑换的 USDT 数量。")

        async def decide_render(self, request):
            return _render_decision(
                source_symbol="USDT", destination_symbol="ETH"
            )

    graph = build_graph(
        model=TypedUsdtKeypadModel(),
        wallet_provider=UsdtWalletProvider(),
        chains={"ETH": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "usdt换eth"},
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "ETH",
                "wallet_accounts": [{"chain": "ETH", "address": "0x" + "1" * 40}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "react-swap-keypad-usdt-balance"}},
    )

    response = result["response"]
    assert response["client_action"]["type"] == "open_amount_keypad"
    slots = response["client_action"]["slots"]
    assert slots["intent"] == "swap"
    assert slots["chain"] == "ETH"
    assert slots["symbol"] == "USDT"
    assert slots["token_address"] == "0xdac17f958d2ee523a2206206994597c13d831ec7"
    assert slots["decimals"] == 6
    assert slots["balance"] == "25.5"
    assert slots["balance_raw"] == "25500000"
    assert slots["balance_usd"] == "25.5"
    assert "portfolio" not in response

    wallet_results = [
        item["result"]
        for item in response["react_results"]
        if item["tool"] == "wallet_balance"
    ]
    assert len(wallet_results) == 1
    asset_row = wallet_results[0]["portfolio"]["assets"][0]
    assert asset_row["asset"]["chain"] == "ETH"
    assert asset_row["asset"]["symbol"] == "USDT"
    assert asset_row["asset"]["address"] == slots["token_address"]
    assert asset_row["amount"] == slots["balance"]
    assert (
        'wallet_balance:{"chain":"ETH","symbol":"USDT"}'
        in result["react_tool_signatures"]
    )


@pytest.mark.asyncio
async def test_typed_swap_keypad_reuses_previously_recommended_token_identity():
    """A typed pair recovers address/decimals from the prior recommendation card."""

    class HistoryUsdtKeypadModel:
        async def invoke_react(self, messages, tools):
            tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "ca-1",
                            "name": "request_client_action",
                            "args": {
                                "action": "open_amount_keypad",
                                "chain": "ETH",
                                "symbol": "USDT",
                                "reason": "请输入要兑换的 USDT 数量",
                            },
                        }
                    ],
                )
            return AIMessage(content="请输入要兑换的 USDT 数量。")

        async def decide_render(self, request):
            return _render_decision(
                source_symbol="USDT", destination_symbol="ETH"
            )

    graph = build_graph(
        model=HistoryUsdtKeypadModel(),
        chains={"ETH": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "usdt换eth"},
            "conversation_history": [
                {"role": "user", "content": "我要兑换"},
                {
                    "role": "assistant",
                    "content": "想兑换哪个币对？",
                    "suggestions": [
                        {
                            "type": "swap_pair",
                            "data": {
                                "intent": "swap",
                                "source": {
                                    "chain": "ETH",
                                    "symbol": "AETHUSDT",
                                    "address": "0x" + "2" * 40,
                                    "decimals": 6,
                                },
                                "destination": {
                                    "chain": "ETH",
                                    "symbol": "USDT",
                                    "address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
                                    "decimals": 6,
                                },
                            },
                        }
                    ],
                },
                {"role": "user", "content": "usdt换eth"},
            ],
        },
        config={"configurable": {"thread_id": "react-swap-keypad-history-identity"}},
    )

    slots = result["response"]["client_action"]["slots"]
    assert slots["intent"] == "swap"
    assert slots["symbol"] == "USDT"
    assert slots["token_address"] == "0xdac17f958d2ee523a2206206994597c13d831ec7"
    assert slots["decimals"] == 6


@pytest.mark.asyncio
async def test_typed_swap_keypad_marks_an_exact_zero_source_balance():
    """A successful scoped lookup with no positive row means a true zero."""

    class ZeroUsdtWalletProvider:
        chain_index_by_name = {"ETH": "1"}

        async def get_token_balances(self, address, indexes):
            return [
                TokenBalance(
                    asset=Asset(
                        chain="ETH",
                        chain_id=1,
                        symbol="USDT",
                        decimals=6,
                        address="0xdac17f958d2ee523a2206206994597c13d831ec7",
                    ),
                    amount=Decimal("0"),
                    amount_raw="0",
                    usd_value=Decimal("0"),
                )
            ]

    class ZeroUsdtKeypadModel:
        async def invoke_react(self, messages, tools):
            tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "ca-1",
                            "name": "request_client_action",
                            "args": {
                                "action": "open_amount_keypad",
                                "chain": "ETH",
                                "symbol": "USDT",
                                "reason": "请输入要兑换的 USDT 数量",
                            },
                        }
                    ],
                )
            return AIMessage(content="请输入要兑换的 USDT 数量。")

        async def decide_render(self, request):
            return _render_decision(
                source_symbol="USDT", destination_symbol="ETH"
            )

    graph = build_graph(
        model=ZeroUsdtKeypadModel(),
        wallet_provider=ZeroUsdtWalletProvider(),
        chains={"ETH": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "usdt换eth"},
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "ETH",
                "wallet_accounts": [{"chain": "ETH", "address": "0x" + "1" * 40}],
            },
            "conversation_history": [
                {
                    "role": "assistant",
                    "content": "想兑换哪个币对？",
                    "suggestions": [
                        {
                            "type": "swap_pair",
                            "data": {
                                "intent": "swap",
                                "destination": {
                                    "chain": "ETH",
                                    "symbol": "USDT",
                                    "address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
                                    "decimals": 6,
                                },
                            },
                        }
                    ],
                },
                {"role": "user", "content": "usdt换eth"},
            ],
        },
        config={"configurable": {"thread_id": "react-swap-keypad-zero"}},
    )

    slots = result["response"]["client_action"]["slots"]
    assert slots["token_address"] == "0xdac17f958d2ee523a2206206994597c13d831ec7"
    assert slots["decimals"] == 6
    assert slots["balance"] == "0"
    assert slots["balance_raw"] == "0"
    assert slots["balance_usd"] == "0"

    wallet_result = next(
        item["result"]
        for item in result["response"]["react_results"]
        if item["tool"] == "wallet_balance"
    )
    assert wallet_result["portfolio"]["assets"] == []
    assert wallet_result["portfolio"]["complete"] is True


@pytest.mark.asyncio
async def test_model_pinned_transfer_keypad_survives_without_user_choice():
    """A model-pinned transfer keypad is the amount step and must be surfaced,
    even when no user coin choice is provable from the message history. The old
    "user-chosen coin" heuristic kept dropping it and left the App with only a
    text bubble (agent_app_bot report), so no fallback overrides the model."""

    class TransferNoCoinModel:
        def __init__(self, decision=None):
            self.decision = decision or _render_decision(intent="transfer")

        async def invoke_react(self, messages, tools):
            tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "ca-1",
                            "name": "request_client_action",
                            "args": {
                                "action": "open_amount_keypad",
                                "chain": "ETH",
                                "symbol": "ETH",
                                "reason": "请输入转账金额",
                            },
                        }
                    ],
                )
            return AIMessage(content="请选择要转账的币种。")

        async def decide_render(self, request):
            return dict(self.decision)

    graph = build_graph(model=TransferNoCoinModel())
    result = await graph.ainvoke(
        {"request": {"message": "转账"}, "conversation_history": []},
        config={"configurable": {"thread_id": "react-transfer-keypad-no-coin"}},
    )

    # The model pinned ETH on the keypad: keep it at the top level so the App
    # renders the amount card.
    action = result["response"].get("client_action")
    assert action is not None
    assert action["type"] == "open_amount_keypad"
    assert action["slots"]["intent"] == "transfer"
    assert action["slots"]["symbol"] == "ETH"
