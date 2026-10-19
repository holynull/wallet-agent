"""Swap channel pinning: App 指定兑换渠道 (provider) via suggestion_data.

Lif (App): “现在默认选择的渠道大多数情况都是 omni…… 能不能把渠道选择放开，我
对接一下 bridgers 包括授权相关的完整流程。” The App passes
``metadata.suggestion_data = {intent: "swap", provider: "bridgers"}`` and the
backend quotes/prepares through only that provider; omitting it keeps the
automatic best-quote selection.
"""

from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from wallet_agent.domain.models import Asset, AssetQuery, NormalizedQuote
from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import _pinned_swap_provider

ETH_SENDER = "0x13c68429D80F56E72C02122652852a1DAfA60AE0"


class FakeProvider:
    provider_name = ""

    CATALOG = {
        ("ETH", "USDT"): Asset(
            chain="ETH",
            symbol="USDT",
            address="0xdac17f958d2ee523a2206206994597c13d831ec7",
            decimals=6,
        ),
        ("TRON", "TRX"): Asset(chain="TRON", symbol="TRX", decimals=6),
    }

    def __init__(self, name):
        self.provider_name = name
        self.quote_calls = 0

    async def list_assets(self, query: AssetQuery):
        chain = str(query.chain or "").upper()
        search = str(query.search or "").upper()
        return [
            asset
            for (asset_chain, asset_symbol), asset in self.CATALOG.items()
            if (not chain or asset_chain.upper() == chain)
            and (not search or search in asset_symbol.upper())
        ]

    async def quote(self, request):
        self.quote_calls += 1
        return NormalizedQuote(
            provider=self.provider_name,
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal("40"),
            expected_output_raw="40000000",
            provider_reference=f"{self.provider_name}-ref",
        )


def _request_with_provider(provider=None):
    suggestion = {"intent": "swap"}
    if provider:
        suggestion["provider"] = provider
    return {
        "message": "把 10 个 ETH 链上的 USDT 换成 TRX",
        "metadata": {"suggestion_data": suggestion},
    }


def _wallet_context():
    return {
        "address": ETH_SENDER,
        "chain": "ETH",
        "wallet_accounts": [{"chain": "ETH", "address": ETH_SENDER}],
    }


def test_pinned_swap_provider_requires_swap_intent_and_enabled_provider():
    providers = {"bridgers": object(), "omnibridge": object()}

    def state(provider=None, intent="swap"):
        suggestion = {"intent": intent}
        if provider:
            suggestion["provider"] = provider
        return {"request": {"metadata": {"suggestion_data": suggestion}}}

    assert _pinned_swap_provider(state(), providers) is None
    assert _pinned_swap_provider(state(provider="bridgers"), providers) == "bridgers"
    assert _pinned_swap_provider(state(provider="OmniBridge"), providers) == "omnibridge"
    assert _pinned_swap_provider(state(provider="unknown"), providers) is None
    assert _pinned_swap_provider(state(provider="bridgers", intent="transfer"), providers) is None
    assert _pinned_swap_provider({}, providers) is None


def test_pinned_swap_provider_honors_provider_without_intent():
    """The App sometimes echoes only ``provider`` (no ``intent``) on a swap step."""
    providers = {"bridgers": object(), "omnibridge": object()}

    def state(suggestion):
        return {"request": {"metadata": {"suggestion_data": suggestion}}}

    assert _pinned_swap_provider(state({"provider": "bridgers"}), providers) == "bridgers"
    assert (
        _pinned_swap_provider(
            state({"intent": "swap_quote", "provider": "omnibridge"}), providers
        )
        == "omnibridge"
    )
    # A strong non-swap intent never pins through this path even with a provider.
    assert (
        _pinned_swap_provider(
            state({"intent": "transfer", "provider": "bridgers"}), providers
        )
        is None
    )


def test_pinned_swap_provider_falls_back_to_active_task():
    """A pin persisted on the active task survives turns that omit ``provider``."""
    providers = {"bridgers": object(), "omnibridge": object()}
    active_task = {"kind": "swap", "status": "collecting", "pinned_provider": "bridgers"}
    assert (
        _pinned_swap_provider(
            {
                "request": {"metadata": {"suggestion_data": {"intent": "swap"}}},
                "active_task": active_task,
            },
            providers,
        )
        == "bridgers"
    )
    # A non-swap task never inherits a persisted pin.
    transfer_task = {"kind": "transfer", "status": "collecting", "pinned_provider": "bridgers"}
    assert (
        _pinned_swap_provider(
            {
                "request": {"metadata": {"suggestion_data": {"intent": "swap"}}},
                "active_task": transfer_task,
            },
            providers,
        )
        is None
    )
    # A completed/cancelled task never keeps a stale pin for a new swap.
    completed_task = {"kind": "swap", "status": "completed", "pinned_provider": "bridgers"}
    assert (
        _pinned_swap_provider(
            {
                "request": {"metadata": {"suggestion_data": {"intent": "swap"}}},
                "active_task": completed_task,
            },
            providers,
        )
        is None
    )


class SwapExtractionModel:
    def __init__(self, output):
        self.output = output

    async def ainvoke(self, _value):
        return self.output


def _swap_output(**overrides):
    return {
        "intent": "swap_quote",
        "source_chain": "ETH",
        "source_symbol": "USDT",
        "destination_chain": "TRON",
        "destination_symbol": "TRX",
        "input_amount": "10",
        **overrides,
    }


@pytest.mark.asyncio
async def test_lifecycle_pin_restricts_quoted_providers_to_the_pinned_one():
    bridgers = FakeProvider("bridgers")
    omnibridge = FakeProvider("omnibridge")
    graph = build_graph(
        model=SwapExtractionModel(_swap_output()),
        providers=[bridgers, omnibridge],
    )
    result = await graph.ainvoke(
        {
            "request": _request_with_provider("bridgers"),
            "wallet_context": _wallet_context(),
        },
        config={"configurable": {"thread_id": "lifecycle-pin-bridgers"}},
    )
    assert bridgers.quote_calls == 1, "pinned provider must be quoted"
    assert omnibridge.quote_calls == 0, "unpinned provider must not be quoted"
    assert result.get("available_providers") == ["bridgers"]


@pytest.mark.asyncio
async def test_lifecycle_without_pin_quotes_all_providers():
    bridgers = FakeProvider("bridgers")
    omnibridge = FakeProvider("omnibridge")
    graph = build_graph(
        model=SwapExtractionModel(_swap_output()),
        providers=[bridgers, omnibridge],
    )
    result = await graph.ainvoke(
        {
            "request": _request_with_provider(None),
            "wallet_context": _wallet_context(),
        },
        config={"configurable": {"thread_id": "lifecycle-no-pin"}},
    )
    assert bridgers.quote_calls == 1
    assert omnibridge.quote_calls == 1
    assert set(result.get("available_providers", [])) == {"bridgers", "omnibridge"}


class ReactQuoteModel:
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
                            "source_symbol": "USDT",
                            "source_chain": "ETH",
                            "destination_symbol": "TRX",
                            "destination_chain": "TRON",
                            "input_amount": "10",
                        },
                    }
                ],
            )
        return AIMessage(content="已为你取到报价。")


@pytest.mark.asyncio
async def test_react_pin_quotes_only_the_pinned_provider():
    bridgers = FakeProvider("bridgers")
    omnibridge = FakeProvider("omnibridge")
    graph = build_graph(
        model=ReactQuoteModel(),
        providers=[bridgers, omnibridge],
    )
    result = await graph.ainvoke(
        {
            "request": _request_with_provider("bridgers"),
            "wallet_context": _wallet_context(),
        },
        config={"configurable": {"thread_id": "react-pin-bridgers"}},
    )
    assert bridgers.quote_calls == 1
    assert omnibridge.quote_calls == 0
    assert result["response"]["kind"] == "react"


@pytest.mark.asyncio
async def test_react_without_pin_quotes_all_providers():
    bridgers = FakeProvider("bridgers")
    omnibridge = FakeProvider("omnibridge")
    graph = build_graph(
        model=ReactQuoteModel(),
        providers=[bridgers, omnibridge],
    )
    result = await graph.ainvoke(
        {
            "request": _request_with_provider(None),
            "wallet_context": _wallet_context(),
        },
        config={"configurable": {"thread_id": "react-no-pin"}},
    )
    assert bridgers.quote_calls == 1
    assert omnibridge.quote_calls == 1
    assert result["response"]["kind"] == "react"


@pytest.mark.asyncio
async def test_lifecycle_pin_persists_across_turns():
    """The pin from the coin-selection turn survives the amount/quote turn."""
    bridgers = FakeProvider("bridgers")
    omnibridge = FakeProvider("omnibridge")
    graph = build_graph(
        model=SwapExtractionModel(_swap_output()),
        providers=[bridgers, omnibridge],
    )
    thread = "lifecycle-pin-cross-turn"
    # Turn 1: the App pins bridgers while the user picks the pair.
    first = await graph.ainvoke(
        {
            "request": _request_with_provider("bridgers"),
            "wallet_context": _wallet_context(),
        },
        config={"configurable": {"thread_id": thread}},
    )
    assert first.get("available_providers") == ["bridgers"]
    assert (first.get("active_task") or {}).get("pinned_provider") == "bridgers"
    # Turn 2: the amount/quote turn repeats no provider, but the task keeps it.
    second = await graph.ainvoke(
        {
            "request": _request_with_provider(None),
            "wallet_context": _wallet_context(),
        },
        config={"configurable": {"thread_id": thread}},
    )
    assert second.get("available_providers") == ["bridgers"]
    assert bridgers.quote_calls == 2
    assert omnibridge.quote_calls == 0


@pytest.mark.asyncio
class ReactCrossTurnModel:
    """Turn 1 opens the amount keypad; turn 2 asks for the quote."""

    async def invoke_react(self, messages, tools):
        tool_names = [
            getattr(item, "name", "")
            for item in messages
            if isinstance(item, ToolMessage)
        ]
        if "get_swap_quote" in tool_names:
            return AIMessage(content="已为你取到报价。")
        if messages and isinstance(messages[-1], ToolMessage):
            return AIMessage(content="已打开金额键盘。")
        last_human = next(
            (item for item in reversed(messages) if isinstance(item, HumanMessage)),
            None,
        )
        text = str(getattr(last_human, "content", "") or "")
        if "金额" in text:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "sq-1",
                        "name": "get_swap_quote",
                        "args": {
                            "source_symbol": "USDT",
                            "source_chain": "ETH",
                            "destination_symbol": "TRX",
                            "destination_chain": "TRON",
                            "input_amount": "10",
                        },
                    }
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "ka-1",
                    "name": "request_client_action",
                    "args": {"action": "open_amount_keypad", "slots": {}},
                }
            ],
        )


@pytest.mark.asyncio
async def test_react_pin_persists_across_turns():
    """ReAct turn 1 pins the channel; turn 2 quotes only through it."""
    bridgers = FakeProvider("bridgers")
    omnibridge = FakeProvider("omnibridge")
    graph = build_graph(
        model=ReactCrossTurnModel(),
        providers=[bridgers, omnibridge],
    )
    thread = "react-pin-cross-turn"
    first = await graph.ainvoke(
        {
            "request": {
                "message": "我想兑换",
                "metadata": {"suggestion_data": {"intent": "swap", "provider": "bridgers"}},
            },
            "wallet_context": _wallet_context(),
        },
        config={"configurable": {"thread_id": thread}},
    )
    assert (first.get("active_task") or {}).get("pinned_provider") == "bridgers"
    assert first["response"]["kind"] == "react"
    second = await graph.ainvoke(
        {
            "request": {
                "message": "输入金额 10",
                "metadata": {"suggestion_data": {"intent": "swap"}},
            },
            "wallet_context": _wallet_context(),
        },
        config={"configurable": {"thread_id": thread}},
    )
    assert bridgers.quote_calls == 1
    assert omnibridge.quote_calls == 0
    assert second["response"]["kind"] == "react"
