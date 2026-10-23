"""A swap request must project the Figma swap-intent card, not a transfer card.

Regression for the App-side report: after a BTC transfer the user said "我要兑换",
the bubble text became a swap clarification but the model still called
``transfer_quick_replies`` (or no tool at all), so the App either rendered the
transfer "input address / pick contact / check balance" card, or nothing at
all. The design (Figma 33715:134319) expects a card that recommends trading
pairs from the user's balance, plus "其他币对" / "跨链换" entries.
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage


def _render_decision(**overrides):
    """A model-owned render decision; the graph only executes on it."""
    decision = {
        "intent": "swap",
        "source_symbol": None,
        "destination_symbol": None,
        "recommend_swap_pairs": False,
        "reason": "test",
    }
    decision.update(overrides)
    return decision


class SwapMisrouteReactModel:
    """Calls ``transfer_quick_replies`` for a swap message, then answers in prose."""

    def __init__(self, decision=None):
        self.decision = decision or _render_decision(recommend_swap_pairs=True)

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
        return AIMessage(content="你想用哪个币种兑换、换成什么？")

    async def decide_render(self, request):
        return dict(self.decision)


class ProseReactModel:
    """Answers every ReAct turn in prose without calling any tool."""

    def __init__(self, decision=None):
        self.decision = decision or _render_decision()

    async def invoke_react(self, messages, tools):
        return AIMessage(content="你想用哪个币种兑换、换成什么？")

    async def decide_render(self, request):
        return dict(self.decision)


def _transfer_chips(suggestions):
    return [
        item
        for item in suggestions
        if str(item.get("type") or "") == "action"
        and str(item.get("value") or "") in {"input_address", "pick_contact", "show_balance"}
    ]


@pytest.mark.asyncio
async def test_react_swap_message_projects_swap_card_not_transfer_card():
    from wallet_agent.graph.build import build_graph

    model = SwapMisrouteReactModel(_render_decision(recommend_swap_pairs=True))
    graph = build_graph(model=model)
    result = await graph.ainvoke(
        {"request": {"message": "我要兑换"}, "conversation_history": []},
        config={"configurable": {"thread_id": "react-swap-drops-transfer-card"}},
    )

    assert result["response"]["kind"] == "react"
    assert "兑换" in result["response"]["message"]
    suggestions = result["response"].get("suggestions") or []
    # The transfer receive-address card must never project over the swap card.
    assert _transfer_chips(suggestions) == []
    # The swap-intent card carries recommended pairs and entry chips.
    assert any(str(item.get("type") or "") == "swap_pair" for item in suggestions)
    assert any(str(item.get("value") or "") == "swap_custom" for item in suggestions)
    assert any(str(item.get("value") or "") == "swap_cross_chain" for item in suggestions)


@pytest.mark.asyncio
async def test_react_open_swap_intent_projects_swap_pair_card_from_defaults():
    """An open "我要兑换" answered in prose must still get a pair card."""
    from wallet_agent.graph.build import build_graph

    graph = build_graph(
        model=ProseReactModel(_render_decision(recommend_swap_pairs=True))
    )
    result = await graph.ainvoke(
        {"request": {"message": "我要兑换"}, "conversation_history": []},
        config={"configurable": {"thread_id": "react-open-swap-card"}},
    )

    suggestions = result["response"].get("suggestions") or []
    pairs = [item for item in suggestions if str(item.get("type") or "") == "swap_pair"]
    assert pairs, "swap intent answered in prose should still return a pair card"
    assert any(
        (item.get("data") or {}).get("source", {}).get("symbol") == "ETH"
        and (item.get("data") or {}).get("destination", {}).get("symbol") == "USDT"
        for item in pairs
    )
    assert any(str(item.get("value") or "") == "swap_custom" for item in suggestions)
    assert any(str(item.get("value") or "") == "swap_cross_chain" for item in suggestions)


@pytest.mark.asyncio
async def test_react_swap_uses_wallet_balances_for_recommendations():
    """Held assets drive the recommended pairs (BASE ETH -> BASE USDT)."""
    from wallet_agent.graph.build import build_graph

    wallet_context = {
        "wallet_accounts": [{"chain": "BASE", "address": "0x" + "1" * 40}],
        "supported_networks": ["BASE"],
        "token_balances": [
            {
                "asset": {
                    "chain": "BASE",
                    "symbol": "ETH",
                    "decimals": 18,
                    "address": "0x" + "2" * 40,
                },
                "amount": "1.5",
                "usd_value": "3000",
            },
            {
                "asset": {
                    "chain": "BASE",
                    "symbol": "USDT",
                    "decimals": 6,
                    "address": "0x" + "3" * 40,
                },
                "amount": "100",
                "usd_value": "100",
            },
        ],
    }
    graph = build_graph(
        model=ProseReactModel(_render_decision(recommend_swap_pairs=True))
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "我要兑换"},
            "conversation_history": [],
            "wallet_context": wallet_context,
        },
        config={"configurable": {"thread_id": "react-swap-balance-card"}},
    )

    suggestions = result["response"].get("suggestions") or []
    pairs = [item for item in suggestions if str(item.get("type") or "") == "swap_pair"]
    assert pairs
    held_pair = next(
        (
            item
            for item in pairs
            if (item.get("data") or {}).get("source", {}).get("chain") == "BASE"
            and (item.get("data") or {}).get("source", {}).get("symbol") == "ETH"
            and (item.get("data") or {}).get("destination", {}).get("symbol") == "USDT"
        ),
        None,
    )
    assert held_pair is not None
    assert (held_pair["data"]["source"].get("address") or "").endswith("2" * 40)


@pytest.mark.asyncio
async def test_react_named_swap_pair_keeps_prose_without_recommendation_card():
    """A message that already names the pair must not get a recommendation card."""
    from wallet_agent.graph.build import build_graph

    graph = build_graph(
        model=ProseReactModel(
            _render_decision(source_symbol="ETH", destination_symbol="USDT")
        )
    )
    result = await graph.ainvoke(
        {"request": {"message": "把 ETH 换成 USDT"}, "conversation_history": []},
        config={"configurable": {"thread_id": "react-named-swap-card"}},
    )

    suggestions = result["response"].get("suggestions") or []
    assert not any(str(item.get("type") or "") == "swap_pair" for item in suggestions)


def _multi_chain_usdt_context():
    return {
        "wallet_accounts": [
            {"chain": "ETH", "address": "0x" + "1" * 40},
            {"chain": "BASE", "address": "0x" + "2" * 40},
        ],
        "supported_networks": ["ETH", "BASE"],
        "token_balances": [
            {
                "asset": {
                    "chain": "ETH",
                    "symbol": "USDT",
                    "decimals": 6,
                    "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
                },
                "amount": "100",
                "usd_value": "100",
            },
            {
                "asset": {
                    "chain": "BASE",
                    "symbol": "USDT",
                    "decimals": 6,
                    "address": "0x" + "3" * 40,
                },
                "amount": "50",
                "usd_value": "50",
            },
        ],
    }


@pytest.mark.asyncio
async def test_swap_multi_chain_source_projects_chain_card():
    """A named pair whose source lives on several networks gets a chain card."""
    from wallet_agent.graph.build import build_graph

    graph = build_graph(
        model=ProseReactModel(
            _render_decision(source_symbol="USDT", destination_symbol="ETH")
        )
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "用 USDT 换 ETH"},
            "conversation_history": [],
            "wallet_context": _multi_chain_usdt_context(),
        },
        config={"configurable": {"thread_id": "react-swap-multichain-usdt"}},
    )

    response = result["response"]
    suggestions = response.get("suggestions") or []
    chain_chips = [item for item in suggestions if str(item.get("type") or "") == "chain"]
    assert len(chain_chips) == 2
    chains = {str(item.get("value") or "") for item in chain_chips}
    assert chains == {"ETH", "BASE"}
    for item in chain_chips:
        data = item.get("data") or {}
        assert data.get("intent") == "swap"
        assert data.get("symbol") == "USDT"
        assert data.get("chain") == item.get("value")
        assert data.get("token_address")
        assert data.get("decimals") == 6
        assert item.get("chain_logo_url")
        assert str(item.get("type") or "") == "chain"
    # No recommended-pair card for an already-named pair.
    assert not any(str(item.get("type") or "") == "swap_pair" for item in suggestions)


@pytest.mark.asyncio
async def test_swap_single_chain_source_no_chain_card():
    """A source coin on only one network needs no chain-selection card."""
    from wallet_agent.graph.build import build_graph

    context = _multi_chain_usdt_context()
    context["token_balances"] = context["token_balances"][:1]
    graph = build_graph(
        model=ProseReactModel(
            _render_decision(source_symbol="USDT", destination_symbol="ETH")
        )
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "用 USDT 换 ETH"},
            "conversation_history": [],
            "wallet_context": context,
        },
        config={"configurable": {"thread_id": "react-swap-singlechain-usdt"}},
    )

    suggestions = result["response"].get("suggestions") or []
    assert not any(str(item.get("type") or "") == "chain" for item in suggestions)


@pytest.mark.asyncio
async def test_swap_pinned_chain_no_chain_card():
    """A tapped chain chip already resolves the ambiguity; do not re-offer it."""
    from wallet_agent.graph.build import build_graph

    graph = build_graph(
        model=ProseReactModel(
            _render_decision(source_symbol="USDT", destination_symbol="ETH")
        )
    )
    result = await graph.ainvoke(
        {
            "request": {
                "message": "用 ETH 网络的 USDT 换 ETH",
                "metadata": {
                    "suggestion_data": {
                        "intent": "swap",
                        "chain": "ETH",
                        "symbol": "USDT",
                        "token_address": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
                        "decimals": 6,
                    }
                },
            },
            "conversation_history": [],
            "wallet_context": _multi_chain_usdt_context(),
        },
        config={"configurable": {"thread_id": "react-swap-pinned-chain"}},
    )

    suggestions = result["response"].get("suggestions") or []
    assert not any(str(item.get("type") or "") == "chain" for item in suggestions)


@pytest.mark.asyncio
async def test_swap_bare_confirmation_keeps_established_pair():
    """After "USDT 换 ETH", a bare "兑换" must keep the pair, not re-recommend."""
    from wallet_agent.graph.build import build_graph

    # The model decides the bare "兑换" continues the established pair.
    graph = build_graph(
        model=ProseReactModel(
            _render_decision(source_symbol="USDT", destination_symbol="ETH")
        )
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "兑换"},
            "conversation_history": [{"role": "user", "content": "USDT 换 ETH"}],
            "wallet_context": _multi_chain_usdt_context(),
        },
        config={"configurable": {"thread_id": "react-swap-bare-confirmation"}},
    )

    suggestions = result["response"].get("suggestions") or []
    # The recommendation card must not bounce back for an already-picked pair.
    assert not any(str(item.get("type") or "") == "swap_pair" for item in suggestions)
    # The source is still ambiguous (USDT on ETH + BASE) -> chain card continues.
    chain_chips = [item for item in suggestions if str(item.get("type") or "") == "chain"]
    assert {str(item.get("value") or "") for item in chain_chips} == {"ETH", "BASE"}
    for item in chain_chips:
        assert (item.get("data") or {}).get("intent") == "swap"
        assert (item.get("data") or {}).get("symbol") == "USDT"


@pytest.mark.asyncio
async def test_swap_fresh_intent_still_recommends_pairs():
    """A fresh "我要兑换" opens a new swap even after an earlier pair."""
    from wallet_agent.graph.build import build_graph

    graph = build_graph(
        model=ProseReactModel(_render_decision(recommend_swap_pairs=True))
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "我要兑换"},
            "conversation_history": [{"role": "user", "content": "USDT 换 ETH"}],
            "wallet_context": _multi_chain_usdt_context(),
        },
        config={"configurable": {"thread_id": "react-swap-fresh-intent"}},
    )

    suggestions = result["response"].get("suggestions") or []
    assert any(str(item.get("type") or "") == "swap_pair" for item in suggestions)
