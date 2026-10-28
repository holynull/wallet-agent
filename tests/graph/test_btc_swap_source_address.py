"""BTC swap source-address selection: ask the App which 出金 address to use.

Lif (App): “出金的地址只能从中间选一个”。A BTC swap's provider-facing
fromAddress/userAddr is a single address, and the App shows each BTC address
separately. When the wallet declared several BTC addresses and none was
explicitly chosen, the backend issues a ``select_btc_address`` client action
instead of silently quoting from the first account.
"""

from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.domain.models import NormalizedQuote
from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import (
    _btc_swap_source_options,
    _react_btc_picker_addresses,
    _swap_requires_btc_source_selection,
)

P2WPKH = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"
P2TR = "bc1p47u7v2k5cpfqt0gxftkat3ade7ucs588g8lr0znzr9le06taty6q6x9gut"
P2SH = "3FGALr2UNebKnBTErhePUk3CuzmDd9FJux"
P2PKH = "1Nqs6wvbYfZK15GQ43ydsiDU7NDnnWR2tm"
EVM = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"


def _wallet_context(accounts):
    return {
        "address": EVM,
        "chain": "ETH",
        "wallet_accounts": [{"chain": "BTC", "address": item} for item in accounts],
    }


def test_btc_swap_source_selection_requires_at_least_two_declared_addresses():
    context = _wallet_context([P2WPKH, P2TR])
    assert _swap_requires_btc_source_selection({"source_chain": "BTC"}, context) == [
        P2WPKH,
        P2TR,
    ]
    assert (
        _swap_requires_btc_source_selection({"source_chain": "BTC"}, _wallet_context([P2WPKH]))
        is None
    )
    assert (
        _swap_requires_btc_source_selection({"source_chain": "BTC"}, _wallet_context([])) is None
    )


def test_btc_swap_source_selection_ignores_non_btc_source_chains():
    context = _wallet_context([P2WPKH, P2TR])
    assert _swap_requires_btc_source_selection({"source_chain": "ETH"}, context) is None
    assert _swap_requires_btc_source_selection({"source_chain": "TRON"}, context) is None


def test_btc_swap_source_selection_skips_when_an_explicit_sender_is_set():
    context = _wallet_context([P2WPKH, P2TR])
    assert (
        _swap_requires_btc_source_selection(
            {"source_chain": "BTC", "sender_address": P2TR}, context
        )
        is None
    )


def test_btc_swap_source_options_only_report_btc_addresses():
    context = _wallet_context([P2WPKH, P2TR, P2SH])
    assert _btc_swap_source_options(context, "BTC") == [P2WPKH, P2TR, P2SH]
    assert _btc_swap_source_options(context, "ETH") == []


class SwapExtractionModel:
    def __init__(self, output):
        self.output = output

    async def ainvoke(self, _value):
        return self.output


def _swap_output(**overrides):
    return {
        "intent": "swap_quote",
        "source_chain": "BTC",
        "source_symbol": "BTC",
        "destination_chain": "ETH",
        "destination_symbol": "ETH",
        **overrides,
    }


@pytest.mark.asyncio
async def test_btc_swap_without_explicit_sender_asks_app_to_select_address():
    graph = build_graph(model=SwapExtractionModel(_swap_output()), providers=[])
    result = await graph.ainvoke(
        {
            "request": {"message": "帮我兑换"},
            "wallet_context": _wallet_context([P2WPKH, P2TR]),
        },
        config={"configurable": {"thread_id": "btc-swap-select-source"}},
    )
    response = result["response"]
    assert response["kind"] == "clarification"
    action = response["client_action"]
    assert action["type"] == "select_btc_address"
    assert action["slots"]["chain"] == "BTC"
    assert action["slots"]["symbol"] == "BTC"
    assert [item["address"] for item in action["slots"]["addresses"]] == [P2WPKH, P2TR]
    assert result["active_task"]["stage"] == "selecting_btc_address"


class FakeProvider:
    provider_name = "bridgers"

    async def quote(self, request):
        return NormalizedQuote(
            provider="bridgers",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal("40"),
            expected_output_raw="40000000",
            provider_reference="bridgers-ref",
        )


@pytest.mark.asyncio
async def test_btc_swap_with_explicit_sender_skips_the_selection_surface():
    graph = build_graph(
        model=SwapExtractionModel(_swap_output(sender_address=P2TR, input_amount="0.0012")),
        providers=[FakeProvider()],
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "帮我兑换"},
            "wallet_context": _wallet_context([P2WPKH, P2TR]),
        },
        config={"configurable": {"thread_id": "btc-swap-explicit-sender"}},
    )
    response = result["response"]
    assert response["kind"] != "clarification" or (
        response.get("client_action") or {}
    ).get("type") != "select_btc_address"


@pytest.mark.asyncio
async def test_btc_swap_uses_address_the_app_returned_via_suggestion_data():
    """The App's picked address lands in the draft even when the model omits it."""
    graph = build_graph(
        model=SwapExtractionModel(_swap_output(input_amount="0.0012")),
        providers=[FakeProvider()],
    )
    result = await graph.ainvoke(
        {
            "request": {
                "message": "帮我兑换",
                "metadata": {
                    "suggestion_data": {"intent": "swap", "sender_address": P2TR}
                },
            },
            "wallet_context": _wallet_context([P2WPKH, P2TR]),
        },
        config={"configurable": {"thread_id": "btc-swap-suggestion-sender"}},
    )
    response = result["response"]
    assert (response.get("client_action") or {}).get("type") != "select_btc_address"
    assert (result.get("swap_draft") or {}).get("sender_address") == P2TR


class SwapQuoteReactModel:
    """ReAct model that quotes BTC -> ETH without naming a 出金 address."""

    async def invoke_react(self, messages, tools):
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "sq-1",
                        "name": "get_swap_quote",
                        "args": {
                            "source_symbol": "BTC",
                            "source_chain": "BTC",
                            "destination_symbol": "ETH",
                            "destination_chain": "ETH",
                            "input_amount": "0.0012",
                        },
                    }
                ],
            )
        return AIMessage(content="请选择出金地址。")


@pytest.mark.asyncio
async def test_react_btc_swap_without_sender_asks_app_to_select_address():
    """ReAct quote on a BTC source without an explicit sender asks the App."""
    graph = build_graph(model=SwapQuoteReactModel(), providers=[])
    result = await graph.ainvoke(
        {
            "request": {"message": "把 BTC 换成 ETH"},
            "wallet_context": _wallet_context([P2WPKH, P2TR]),
        },
        config={"configurable": {"thread_id": "react-btc-swap-select-source"}},
    )
    response = result["response"]
    assert response["kind"] == "react"
    action = response.get("client_action")
    assert action is not None, "BTC swap without a sender must ask the App for one"
    assert action["type"] == "select_btc_address"
    assert [item["address"] for item in action["slots"]["addresses"]] == [P2WPKH, P2TR]


class SelectAddressThenKeypadModel:
    """Requests the BTC address surface and the amount keypad in one turn."""

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
                            "action": "select_btc_address",
                            "chain": "BTC",
                            "reason": "BTC 兑换需要指定出金地址",
                        },
                    },
                    {
                        "id": "ca-2",
                        "name": "request_client_action",
                        "args": {
                            "action": "open_amount_keypad",
                            "chain": "BTC",
                            "symbol": "BTC",
                            "reason": "请输入要兑换的 BTC 金额",
                        },
                    },
                ],
            )
        return AIMessage(content="请选择出金地址并输入金额。")


@pytest.mark.asyncio
async def test_btc_address_surface_outranks_the_amount_keypad():
    """When both are requested, the 出金地址 picker comes first."""
    graph = build_graph(model=SelectAddressThenKeypadModel(), providers=[])
    result = await graph.ainvoke(
        {
            "request": {"message": "把 BTC 换成 USDT"},
            "wallet_context": _wallet_context([P2WPKH, P2TR]),
        },
        config={"configurable": {"thread_id": "react-btc-address-before-keypad"}},
    )
    action = result["response"].get("client_action")
    assert action is not None
    assert action["type"] == "select_btc_address"


class TrxToBtcReactModel:
    """Model mistakenly asks for a BTC 出金地址 on a TRX -> BTC swap."""

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
                            "action": "select_btc_address",
                            "chain": "TRON",
                            "symbol": "TRX",
                            "reason": "TRX 换成 BTC 需要选定出金地址",
                        },
                    },
                    {
                        "id": "sq-1",
                        "name": "get_swap_quote",
                        "args": {
                            "source_symbol": "TRX",
                            "source_chain": "TRON",
                            "destination_symbol": "BTC",
                            "destination_chain": "BTC",
                            "input_amount": "70.5",
                        },
                    },
                ],
            )
        return AIMessage(content="TRON 上的 TRX 全部换成 BTC 的报价已生成。")


@pytest.mark.asyncio
async def test_model_requested_btc_picker_is_respected():
    """The model is the authority on ``select_btc_address``: an explicit picker
    request is surfaced even when a heuristic could argue against it (here a
    TRX → BTC swap). The previous hardcoded drop kept misfiring and left the App
    with no card (agent_app_bot follow-ups), so no fallback overrides the model.
    """
    graph = build_graph(
        model=TrxToBtcReactModel(),
        providers=[FakeProvider()],
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "全部 TRX 换成 BTC"},
            "wallet_context": _wallet_context([P2WPKH, P2TR]),
        },
        config={"configurable": {"thread_id": "react-trx-to-btc-no-selector"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert (response.get("client_action") or {}).get("type") == "select_btc_address"
    assert any(
        item.get("tool") == "get_swap_quote" for item in response.get("react_results") or []
    )


class ProseRepeatSwapModel:
    """Model answers a repeat swap request in prose without calling tools."""

    async def invoke_react(self, messages, tools):
        return AIMessage(
            content="TRX 全部换 BTC 的报价如下，请在卡片上确认并签名。"
        )

    async def decide_render(self, request):
        return {
            "intent": "swap",
            "source_symbol": "TRX",
            "destination_symbol": "BTC",
            "recommend_swap_pairs": False,
            "reason": "test",
        }


@pytest.mark.asyncio
async def test_repeat_swap_prose_reprojects_existing_quote_candidates():
    """If the model reuses checkpoint quotes and only writes prose, the backend
    must re-select a default quote so the API still shows the confirmation card."""
    graph = build_graph(model=ProseRepeatSwapModel(), providers=[])
    result = await graph.ainvoke(
        {
            "request": {"message": "全部 TRX 换成 BTC"},
            "wallet_context": _wallet_context([P2WPKH, P2TR]),
            "swap_request": {
                "source_asset": {"chain": "TRON", "symbol": "TRX"},
                "destination_asset": {"chain": "BTC", "symbol": "BTC"},
                "input_amount": "70.553436",
            },
            "quote_candidates": [
                {
                    "provider": "bridgers",
                    "provider_reference": "quote-bridgers",
                    "input_amount": "70.553436",
                    "expected_output": "0.000262",
                },
                {
                    "provider": "omnibridge",
                    "provider_reference": "quote-omni",
                    "input_amount": "70.553436",
                    "expected_output": "0.0002627",
                },
            ],
        },
        config={"configurable": {"thread_id": "react-repeat-swap-prose"}},
    )

    assert result.get("fresh_swap_projected") is True
    assert (result.get("selected_quote") or {}).get("provider") == "omnibridge"


class TypedBtcPairModel:
    """Model asks for the BTC source picker and the amount keypad in one turn
    after the user typed the pair in prose (no suggestion_data echo)."""

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
                            "action": "select_btc_address",
                            "chain": "BTC",
                            "symbol": "BTC",
                            "intent": "swap",
                            "reason": "BTC is the source asset of the swap; "
                            "choose which BTC address to send from.",
                        },
                    },
                    {
                        "id": "ca-2",
                        "name": "request_client_action",
                        "args": {
                            "action": "open_amount_keypad",
                            "chain": "BTC",
                            "symbol": "BTC",
                            "intent": "swap",
                            "reason": "Enter the BTC amount to swap to USDT.",
                        },
                    },
                ],
            )
        return AIMessage(
            content="Your BTC balance is 0.00038502 BTC. Please pick which BTC "
            "address to send from and enter the amount you want to swap to USDT."
        )


@pytest.mark.asyncio
async def test_react_btc_picker_survives_chainless_chinese_typed_pair():
    """agent_app_bot follow-up (run f5233bd6): Chinese "把 BTC 换成 USDT" with
    the model calling ``select_btc_address`` without a ``slots.chain`` (only
    ``symbol: "BTC"``) must still surface the picker with chain recovered and
    ``addresses`` filled from the declared BTC accounts (no wallet_balance
    call this turn). The previous strict chain sanity check dropped it."""

    class ChainlessBtcPickerModel:
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
                                "action": "select_btc_address",
                                "symbol": "BTC",
                                "intent": "swap",
                                "reason": "BTC 是卖出方，需要选择出金地址",
                            },
                        },
                        {
                            "id": "ca-2",
                            "name": "request_client_action",
                            "args": {
                                "action": "open_amount_keypad",
                                "symbol": "BTC",
                                "intent": "swap",
                                "reason": "输入要兑换的 BTC 数量",
                            },
                        },
                    ],
                )
            return AIMessage(
                content="你有一个 BTC 地址列表，请先选择用哪个 BTC 地址出金，"
                "然后输入要兑换的 BTC 数量。"
            )

    graph = build_graph(
        model=ChainlessBtcPickerModel(),
        chains={"BTC": object(), "ETH": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "把 BTC 换成 USDT"},
            "wallet_context": _wallet_context([P2WPKH, P2TR, P2SH, P2PKH]),
        },
        config={"configurable": {"thread_id": "react-chainless-btc-picker"}},
    )

    response = result["response"]
    action = response.get("client_action")
    assert action is not None
    assert action["type"] == "select_btc_address"
    assert action["slots"]["chain"] == "BTC"
    assert action["slots"]["symbol"] == "BTC"
    assert action["slots"]["intent"] == "swap"
    assert [item["address"] for item in action["slots"]["addresses"]] == [
        P2WPKH,
        P2TR,
        P2SH,
        P2PKH,
    ]
    # Only the picker is elevated; the amount keypad stays in react_results.
    assert response["client_action"]["type"] == "select_btc_address"
    keypad_results = [
        item
        for item in response.get("react_results") or []
        if item.get("result", {}).get("client_action", {}).get("type")
        == "open_amount_keypad"
    ]
    assert len(keypad_results) == 1


@pytest.mark.asyncio
async def test_react_btc_source_picker_survives_typed_pair_prose():
    """agent_app_bot report: typing "Swap BTC to USDT" (no tapped card, no
    quote/draft yet) must still surface the top-level ``select_btc_address``
    card with ``slots.addresses``; the amount keypad waits for the next turn.

    Previously the picker was dropped because ``_swap_source_chain_is_btc``
    returned False when the source was merely unknown (no suggestion_data,
    quote or draft), leaving the App with only a text bubble.
    """
    graph = build_graph(
        model=TypedBtcPairModel(),
        chains={"BTC": object(), "ETH": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "Swap BTC to USDT"},
            "wallet_context": _wallet_context([P2WPKH, P2TR, P2SH, P2PKH]),
        },
        config={"configurable": {"thread_id": "react-typed-btc-pair-picker"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    action = response.get("client_action")
    assert action is not None
    assert action["type"] == "select_btc_address"
    assert action["slots"]["chain"] == "BTC"
    assert action["slots"]["symbol"] == "BTC"
    assert action["slots"]["intent"] == "swap"
    assert [item["address"] for item in action["slots"]["addresses"]] == [
        P2WPKH,
        P2TR,
        P2SH,
        P2PKH,
    ]
    # Only the picker is elevated; the amount keypad stays inside react_results
    # until the user picks an address.
    assert response["client_action"]["type"] == "select_btc_address"
    keypad_results = [
        item
        for item in response.get("react_results") or []
        if item.get("result", {}).get("client_action", {}).get("type")
        == "open_amount_keypad"
    ]
    assert len(keypad_results) == 1


def test_react_btc_picker_addresses_prefer_wallet_balance_rows():
    """The picker's address list comes from the same turn's wallet_balance
    network_status (the report's run carried 4 BTC addresses there), falling
    back to the App-declared accounts."""
    wallet_balance_payload = {
        "ok": True,
        "portfolio": {
            "network_status": [
                {
                    "chain": "BTC",
                    "addresses": [
                        "bc1q8aw7823qeneuayzzz69khl6hsvygwkwg70hnu7",
                        "bc1p5z74y9aee7dkfr6zzmu3y50zvqvfwfm7s7hcnftrwc53haema7wqshz62l",
                        "37socxkGRYM3rVBLcPccVCF3LuP4JvvvBn",
                        "1Nqs6wvbYfZK15GQ43ydsiDU7NDnnWR2tm",
                    ],
                },
                {"chain": "ETH", "addresses": ["0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"]},
            ],
            "wallet": {
                "native_balance": {
                    "addresses": ["bc1q8aw7823qeneuayzzz69khl6hsvygwkwg70hnu7"]
                }
            },
        },
    }
    tool_payloads = [("wallet_balance", wallet_balance_payload)]
    context = _wallet_context([P2WPKH, P2TR])
    assert _react_btc_picker_addresses(tool_payloads, context) == [
        "bc1q8aw7823qeneuayzzz69khl6hsvygwkwg70hnu7",
        "bc1p5z74y9aee7dkfr6zzmu3y50zvqvfwfm7s7hcnftrwc53haema7wqshz62l",
        "37socxkGRYM3rVBLcPccVCF3LuP4JvvvBn",
        "1Nqs6wvbYfZK15GQ43ydsiDU7NDnnWR2tm",
    ]
    assert _react_btc_picker_addresses([], context) == [P2WPKH, P2TR]
