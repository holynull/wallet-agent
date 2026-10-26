"""Design v1.6.9 transfer card: quick replies carry an explicit type and value."""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import _sanitize_response_suggestions

EVM = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"


class FakeModel:
    async def ainvoke(self, value):
        return {"intent": value.get("intent", "clarification")}


def _render_decision(**overrides):
    decision = {
        "intent": "transfer",
        "source_symbol": None,
        "destination_symbol": None,
        "recommend_swap_pairs": False,
        "reason": "test",
    }
    decision.update(overrides)
    return decision


async def _clarify(thread_id: str, wallet_context: dict) -> dict:
    graph = build_graph(model=FakeModel())
    result = await graph.ainvoke(
        {
            "request": {"message": "转 1 USDC", "address": EVM, "chain": "BASE"},
            "intent": "transfer",
            "transfer_draft": {"chain": "BASE", "symbol": "ETH", "amount": "0.01"},
            "wallet_context": wallet_context,
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    return result["response"]


@pytest.mark.asyncio
async def test_missing_recipient_offers_a_typed_three_chip_row():
    response = await _clarify(
        "transfer-chips",
        {"address": EVM, "chain": "BASE", "wallet_accounts": [{"chain": "BASE", "address": EVM}]},
    )

    assert response["kind"] == "clarification"
    assert response["missing_fields"] == ["transfer_recipient"]
    # The design's address-input card always leads with 输入地址; the wallet's
    # own address must not become the default recipient.
    assert [item["type"] for item in response["suggestions"]] == ["action", "action", "action"]
    assert [item["value"] for item in response["suggestions"]] == [
        "input_address",
        "pick_contact",
        "show_balance",
    ]
    # The chips still carry the slot data a client needs to continue, and none
    # of them preselects the connected wallet as the recipient.
    assert [item["data"]["intent"] for item in response["suggestions"]] == ["transfer"] * 3
    assert not any(item["data"].get("recipient") for item in response["suggestions"])


@pytest.mark.asyncio
async def test_missing_recipient_without_a_connected_account_offers_the_input_action():
    response = await _clarify("transfer-chips-no-wallet", {"chain": "BASE"})

    assert [item["type"] for item in response["suggestions"]] == ["action", "action", "action"]
    assert response["suggestions"][0]["value"] == "input_address"
    assert [item["value"] for item in response["suggestions"][1:]] == [
        "pick_contact",
        "show_balance",
    ]


class ChipModel:
    """Asks for the standard transfer chips before replying."""

    async def invoke_react(self, messages, tools):
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[{"id": "qr-1", "name": "transfer_quick_replies", "args": {}}],
            )
        return AIMessage(content="转给谁？给我个地址 👇")


@pytest.mark.asyncio
async def test_react_turn_surfaces_the_quick_reply_chips_on_the_response():
    graph = build_graph(model=ChipModel())
    result = await graph.ainvoke(
        {
            "request": {"message": "转一笔帐给朋友", "address": EVM, "chain": "BASE"},
            "wallet_context": {
                "address": EVM,
                "chain": "BASE",
                "wallet_accounts": [{"chain": "BASE", "address": EVM}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "react-chips"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert [item["type"] for item in response["suggestions"]] == ["action", "action", "action"]
    assert response["suggestions"][0]["value"] == "input_address"
    assert response["suggestions"][1]["value"] == "pick_contact"


@pytest.mark.asyncio
async def test_missing_amount_offers_balance_percentage_presets():
    graph = build_graph(model=FakeModel())
    result = await graph.ainvoke(
        {
            "request": {"message": "转 0x…", "address": EVM, "chain": "BASE"},
            "intent": "transfer",
            "transfer_draft": {
                "chain": "BASE",
                "symbol": "ETH",
                "recipient": "0x" + "2" * 40,
            },
            "wallet_context": {
                "address": EVM,
                "chain": "BASE",
                "wallet_accounts": [{"chain": "BASE", "address": EVM}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "transfer-amount-presets"}},
    )

    response = result["response"]
    assert response["kind"] == "clarification"
    assert response["missing_fields"] == ["transfer_amount"]
    assert [(item["type"], item["value"]) for item in response["suggestions"]] == [
        ("amount", "25%"),
        ("amount", "50%"),
        ("amount", "75%"),
        ("amount", "100%"),
    ]


@pytest.mark.asyncio
async def test_missing_amount_without_symbol_offers_a_coin_card_first():
    """The amount presets are meaningless before the coin is known: ask for the
    coin (native + wallet tokens) first so the later amount card has a balance."""
    graph = build_graph(model=FakeModel())
    result = await graph.ainvoke(
        {
            "request": {"message": "转 0x…", "address": EVM, "chain": "BASE"},
            "intent": "transfer",
            "transfer_draft": {
                "chain": "BASE",
                "recipient": "0x" + "2" * 40,
            },
            "wallet_context": {
                "address": EVM,
                "chain": "BASE",
                "wallet_accounts": [{"chain": "BASE", "address": EVM}],
                "token_balances": [
                    {
                        "asset": {
                            "chain": "BASE",
                            "symbol": "USDC",
                            "address": "0x" + "3" * 40,
                            "decimals": 6,
                        }
                    }
                ],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "transfer-coin-card"}},
    )

    response = result["response"]
    assert response["kind"] == "clarification"
    assert response["missing_fields"] == ["transfer_amount"]
    suggestions = response["suggestions"]
    assert [(item["type"], item["value"]) for item in suggestions] == [
        ("symbol", "ETH"),
        ("symbol", "USDC"),
    ]
    assert suggestions[0]["data"] == {
        "intent": "transfer",
        "chain": "BASE",
        "symbol": "ETH",
        "decimals": 18,
    }
    assert suggestions[1]["data"]["token_address"] == "0x" + "3" * 40
    assert suggestions[1]["data"]["decimals"] == 6


@pytest.mark.asyncio
async def test_missing_amount_with_draft_token_address_carries_token_identity():
    """A coin chosen via the coin card (symbol + token_address in the draft)
    must carry its address and decimals onto the amount card."""
    graph = build_graph(model=FakeModel())
    result = await graph.ainvoke(
        {
            "request": {"message": "转 0x…", "address": EVM, "chain": "BASE"},
            "intent": "transfer",
            "transfer_draft": {
                "chain": "BASE",
                "symbol": "USDC",
                "token_address": "0x" + "3" * 40,
                "decimals": 6,
                "recipient": "0x" + "2" * 40,
            },
            "wallet_context": {
                "address": EVM,
                "chain": "BASE",
                "wallet_accounts": [{"chain": "BASE", "address": EVM}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "transfer-amount-token-identity"}},
    )

    response = result["response"]
    assert response["kind"] == "clarification"
    assert response["missing_fields"] == ["transfer_amount"]
    amount = response["suggestions"][0]
    assert amount["type"] == "amount"
    assert amount["data"]["symbol"] == "USDC"
    assert amount["data"]["token_address"] == "0x" + "3" * 40
    assert amount["data"]["decimals"] == 6


@pytest.mark.asyncio
async def test_missing_chain_offers_only_a_chain_card_with_current_network_first():
    class ChainCardModel:
        """Leaves the chain unanswered so transfer_quick_replies returns the
        chain card with the connected network first."""

        async def invoke_react(self, messages, tools):
            if not [m for m in messages if isinstance(m, ToolMessage)]:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "qr-1",
                            "name": "transfer_quick_replies",
                            "args": {"symbol": "USDT", "recipient": EVM},
                        }
                    ],
                )
            return AIMessage(content="要转哪条链？ 👇")

    graph = build_graph(
        model=ChainCardModel(),
        chains={"BASE": object(), "ETH": object(), "BSC": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "转给 0x… USDT", "address": EVM, "chain": "ETH"},
            "wallet_context": {
                "address": EVM,
                "chain": "ETH",
                "wallet_accounts": [{"chain": "ETH", "address": EVM}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "transfer-chain-card"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    # The chain card lists the supported chains with the connected network
    # first, and does not mix in amount presets on this step.
    assert [(item["type"], item["value"]) for item in response["suggestions"]] == [
        ("chain", "ETH"),
        ("chain", "BASE"),
        ("chain", "BSC"),
    ]


@pytest.mark.asyncio
async def test_missing_chain_with_tron_recipient_offers_only_matching_chains():
    """A TRON recipient must not be offered EVM networks (Lif's report)."""

    class ChainCardModel:
        async def invoke_react(self, messages, tools):
            if not [m for m in messages if isinstance(m, ToolMessage)]:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "qr-1",
                            "name": "transfer_quick_replies",
                            "args": {
                                "recipient": "TPtbB3MT4o7rQ7vpEMW8BWquwfJvoAkHia"
                            },
                        }
                    ],
                )
            return AIMessage(content="要转哪条链？ 👇")

    graph = build_graph(
        model=ChainCardModel(),
        chains={
            "TRON": object(),
            "ETH": object(),
            "BASE": object(),
            "BSC": object(),
        },
    )
    result = await graph.ainvoke(
        {
            "request": {
                "message": "转给 TPtbB3MT4o7rQ7vpEMW8BWquwfJvoAkHia",
                "address": EVM,
                "chain": "ETH",
            },
            "wallet_context": {
                "address": EVM,
                "chain": "ETH",
                "wallet_accounts": [{"chain": "ETH", "address": EVM}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "transfer-chain-card-tron"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    chain_values = [
        item["value"] for item in response["suggestions"] if item["type"] == "chain"
    ]
    assert chain_values == ["TRON"]


@pytest.mark.asyncio
async def test_react_text_only_answer_after_address_still_surfaces_chain_card():
    """The chain card must appear even when the model answers the address with prose."""

    class ProseDecisionModel:
        async def invoke_react(self, messages, tools):
            return AIMessage(content="收到收款地址了，请选择网络：ETH、BSC、BASE……")

        async def decide_render(self, request):
            return _render_decision(intent="transfer")

    graph = build_graph(
        model=ProseDecisionModel(),
        chains={"BASE": object(), "ETH": object(), "BSC": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": EVM},
            "conversation_history": [
                {"role": "user", "content": "转一笔帐给朋友"},
                {
                    "role": "assistant",
                    "content": "好的，先确认收款方。你想怎么提供对方的收款地址？",
                },
            ],
            "wallet_context": {
                "address": EVM,
                "chain": "ETH",
                "wallet_accounts": [{"chain": "ETH", "address": EVM}],
            },
        },
        config={"configurable": {"thread_id": "react-chain-card-fallback"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert [(item["type"], item["value"]) for item in response["suggestions"]] == [
        ("chain", "ETH"),
        ("chain", "BASE"),
        ("chain", "BSC"),
    ]


@pytest.mark.asyncio
async def test_react_prose_address_without_recipient_prompt_does_not_inject_chain_card():
    """A bare address alone is not enough: the previous turn must have asked for a recipient."""

    class ProseDecisionModel:
        async def invoke_react(self, messages, tools):
            return AIMessage(content="这个地址是 EVM 地址，可以走以太坊系列网络。")

        async def decide_render(self, request):
            return _render_decision(intent="other")

    graph = build_graph(
        model=ProseDecisionModel(),
        chains={"BASE": object(), "ETH": object(), "BSC": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": EVM},
            "conversation_history": [
                {"role": "user", "content": "帮我看下这个地址"},
                {"role": "assistant", "content": "这是哪个网络的地址？"},
            ],
        },
        config={"configurable": {"thread_id": "react-no-chain-card"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert "suggestions" not in response


@pytest.mark.asyncio
async def test_react_text_only_after_chain_still_surfaces_coin_card():
    """After the chain is selected the model sometimes answers in prose without
    calling transfer_quick_replies; the coin card must still appear so the
    amount card later has a symbol and balance to anchor to."""

    class ProseDecisionModel:
        async def invoke_react(self, messages, tools):
            return AIMessage(content="网络已选 ETH，最后请提供转账金额和币种。")

        async def decide_render(self, request):
            return _render_decision(intent="transfer")

    graph = build_graph(
        model=ProseDecisionModel(),
        chains={"BASE": object(), "ETH": object(), "BSC": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {
                "message": "ETH",
                "metadata": {"suggestion_data": {"intent": "transfer", "chain": "ETH"}},
            },
            "conversation_history": [
                {"role": "user", "content": "转一笔帐给朋友"},
                {
                    "role": "assistant",
                    "content": "好的，先确认收款方。你想怎么提供对方的收款地址？",
                },
                {"role": "user", "content": "0x1F6E4FD185ce8C8171b4BDd2A64667BC69Aff11A"},
                {"role": "assistant", "content": "已记录收款地址，请选择网络。"},
            ],
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "ETH",
                "wallet_accounts": [{"chain": "ETH", "address": "0x" + "1" * 40}],
            },
        },
        config={"configurable": {"thread_id": "react-coin-card-fallback"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert [(item["type"], item["value"]) for item in response["suggestions"]] == [
        ("symbol", "ETH"),
    ]
    assert response["suggestions"][0]["data"]["symbol"] == "ETH"


@pytest.mark.asyncio
async def test_react_keypad_with_model_default_symbol_keeps_coin_card():
    """The model defaulting the chain's native coin into the keypad is not a
    user choice; drop the keypad so the coin-selection card still appears."""

    class BothToolsModel:
        async def invoke_react(self, messages, tools):
            tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "qr-1",
                            "name": "transfer_quick_replies",
                            "args": {"chain": "BSC", "recipient": "0x" + "2" * 40},
                        },
                        {
                            "id": "act-1",
                            "name": "request_client_action",
                            "args": {
                                "action": "open_amount_keypad",
                                "chain": "BSC",
                                "symbol": "BNB",
                                "reason": "输入金额",
                            },
                        },
                    ],
                )
            return AIMessage(content="网络已选 BSC，请输入转账金额。")

    graph = build_graph(
        model=BothToolsModel(),
        chains={"BASE": object(), "ETH": object(), "BSC": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "BSC"},
            "conversation_history": [
                {"role": "user", "content": "转一笔帐给朋友"},
                {
                    "role": "assistant",
                    "content": "好的，先确认收款方。你想怎么提供对方的收款地址？",
                },
                {"role": "user", "content": "0x1F6E4FD185ce8C8171b4BDd2A64667BC69Aff11A"},
                {"role": "assistant", "content": "已记录收款地址，请选择网络。"},
            ],
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "BSC",
                "wallet_accounts": [{"chain": "BSC", "address": "0x" + "1" * 40}],
            },
        },
        config={"configurable": {"thread_id": "react-keypad-reconcile"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert "client_action" not in response
    assert [(item["type"], item["value"]) for item in response["suggestions"]] == [
        ("symbol", "BNB"),
    ]


@pytest.mark.asyncio
async def test_react_keypad_after_user_picked_coin_keeps_keypad():
    """When the user actually picked a coin (suggestion_data.symbol), the keypad
    is the amount step: drop the coin chip and carry decimals on the keypad."""

    class UserPickedCoinModel:
        async def invoke_react(self, messages, tools):
            tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "qr-1",
                            "name": "transfer_quick_replies",
                            "args": {
                                "chain": "BSC",
                                "symbol": "BNB",
                                "recipient": "0x" + "2" * 40,
                            },
                        },
                        {
                            "id": "act-1",
                            "name": "request_client_action",
                            "args": {
                                "action": "open_amount_keypad",
                                "chain": "BSC",
                                "symbol": "BNB",
                                "reason": "输入金额",
                            },
                        },
                    ],
                )
            return AIMessage(content="网络 BSC、币种 BNB，请输入转账金额。")

    graph = build_graph(
        model=UserPickedCoinModel(),
        chains={"BASE": object(), "ETH": object(), "BSC": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {
                "message": "转 BNB",
                "metadata": {
                    "suggestion_data": {
                        "intent": "transfer",
                        "chain": "BSC",
                        "symbol": "BNB",
                    }
                },
            },
            "conversation_history": [
                {"role": "user", "content": "转一笔帐给朋友"},
                {
                    "role": "assistant",
                    "content": "好的，先确认收款方。你想怎么提供对方的收款地址？",
                },
                {"role": "user", "content": "0x1F6E4FD185ce8C8171b4BDd2A64667BC69Aff11A"},
                {"role": "assistant", "content": "已记录收款地址，请选择网络。"},
            ],
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "BSC",
                "wallet_accounts": [{"chain": "BSC", "address": "0x" + "1" * 40}],
            },
        },
        config={"configurable": {"thread_id": "react-keypad-user-coin"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert [item["type"] for item in response["suggestions"]] == ["amount"] * 4
    assert response["client_action"]["type"] == "open_amount_keypad"
    assert response["client_action"]["slots"]["symbol"] == "BNB"
    assert response["client_action"]["slots"]["decimals"] == 18


@pytest.mark.asyncio
async def test_react_keypad_without_symbol_keeps_coin_card():
    """A keypad with no symbol cannot anchor a raw amount; drop it and let the
    coin card drive the next step."""

    class KeypadNoSymbolModel:
        async def invoke_react(self, messages, tools):
            tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "qr-1",
                            "name": "transfer_quick_replies",
                            "args": {"chain": "BSC", "recipient": "0x" + "2" * 40},
                        },
                        {
                            "id": "act-1",
                            "name": "request_client_action",
                            "args": {
                                "action": "open_amount_keypad",
                                "chain": "BSC",
                                "reason": "输入金额",
                            },
                        },
                    ],
                )
            return AIMessage(content="请输入转账金额和币种。")

    graph = build_graph(
        model=KeypadNoSymbolModel(),
        chains={"BASE": object(), "ETH": object(), "BSC": object()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "BSC"},
            "conversation_history": [
                {"role": "user", "content": "转一笔帐给朋友"},
                {
                    "role": "assistant",
                    "content": "好的，先确认收款方。你想怎么提供对方的收款地址？",
                },
                {"role": "user", "content": "0x1F6E4FD185ce8C8171b4BDd2A64667BC69Aff11A"},
                {"role": "assistant", "content": "已记录收款地址，请选择网络。"},
            ],
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "BSC",
                "wallet_accounts": [{"chain": "BSC", "address": "0x" + "1" * 40}],
            },
        },
        config={"configurable": {"thread_id": "react-keypad-no-symbol"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert "client_action" not in response
    assert [(item["type"], item["value"]) for item in response["suggestions"]] == [
        ("symbol", "BNB"),
    ]


@pytest.mark.asyncio
async def test_react_two_turn_address_then_prose_surfaces_chain_card():
    """Mirrors the real /v1/agent/turn flow: the chain card survives a two-turn
    exchange where the model answers the address with prose and never calls
    transfer_quick_replies on the second turn."""
    from langgraph.checkpoint.memory import MemorySaver

    class TwoTurnTransferModel:
        def __init__(self):
            self.calls = 0

        async def invoke_react(self, messages, tools):
            self.calls += 1
            tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "qr-1", "name": "transfer_quick_replies", "args": {}}
                    ],
                )
            if self.calls == 2:
                return AIMessage(content="好的，先确认收款方。你想怎么提供对方的收款地址？")
            return AIMessage(content="收到收款地址了，请选择网络：ETH、BSC、BASE……")

        async def decide_render(self, request):
            return _render_decision(intent="transfer")

    graph = build_graph(
        model=TwoTurnTransferModel(),
        chains={"BASE": object(), "ETH": object(), "BSC": object()},
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "two-turn-chain-card"}}
    wallet_context = {
        "address": EVM,
        "chain": "ETH",
        "wallet_accounts": [{"chain": "ETH", "address": EVM}],
        "supported_networks": ["ETH", "BASE", "BSC"],
    }
    await graph.ainvoke(
        {"request": {"message": "转一笔帐给朋友"}, "wallet_context": wallet_context},
        config=config,
    )
    # The API appends the current user message every turn and relies on the
    # checkpointer to keep the earlier exchange, exactly like the live flow.
    second = await graph.ainvoke(
        {
            "request": {"message": EVM},
            "messages": [{"role": "user", "content": EVM}],
            "conversation_history": [{"role": "user", "content": EVM}],
            "wallet_context": wallet_context,
        },
        config=config,
    )

    response = second["response"]
    assert response["kind"] == "react"
    assert response["route_reason"] == "model_answer"
    assert [(item["type"], item["value"]) for item in response["suggestions"]] == [
        ("chain", "ETH"),
        ("chain", "BASE"),
        ("chain", "BSC"),
    ]


@pytest.mark.asyncio
async def test_missing_amount_carries_the_wallet_asset_identity():
    token_address = "0x" + "4" * 40
    graph = build_graph(model=FakeModel(), chains={"BASE": object()})
    result = await graph.ainvoke(
        {
            "request": {"message": "转给 0x… BASE 链 USDT", "address": EVM, "chain": "BASE"},
            "intent": "transfer",
            "transfer_draft": {
                "chain": "BASE",
                "symbol": "USDT",
                "recipient": "0x" + "2" * 40,
            },
            "wallet_context": {
                "address": EVM,
                "chain": "BASE",
                "wallet_accounts": [{"chain": "BASE", "address": EVM}],
                "token_balances": [
                    {
                        "asset": {
                            "chain": "BASE",
                            "symbol": "USDT",
                            "address": token_address,
                            "decimals": 18,
                        },
                        "amount": "12.5",
                    }
                ],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "transfer-amount-asset"}},
    )

    response = result["response"]
    assert response["missing_fields"] == ["transfer_amount"]
    suggestions = response["suggestions"]
    assert [(item["type"], item["value"]) for item in suggestions] == [
        ("amount", "25%"),
        ("amount", "50%"),
        ("amount", "75%"),
        ("amount", "100%"),
    ]
    # The amount card pins the exact token so the App can render the asset line.
    assert all(item["data"]["token_address"] == token_address for item in suggestions)
    assert all(item["data"]["decimals"] == 18 for item in suggestions)


def test_provider_alias_still_matches_the_app_declared_chain():
    facts = {"supported_chains": ["ETH", "ARBITRUM"], "supported_assets": [], "balances": []}

    kept = _sanitize_response_suggestions(
        [
            {
                "label": "用 ARB",
                "message": "用 ARB 转",
                "data": {"intent": "transfer", "chain": "ARB"},
            }
        ],
        intent="transfer",
        facts=facts,
    )
    assert [item["data"]["chain"] for item in kept] == ["ARB"]

    dropped = _sanitize_response_suggestions(
        [
            {
                "label": "用 APTOS",
                "message": "用 APTOS 转",
                "data": {"intent": "transfer", "chain": "APTOS"},
            }
        ],
        intent="transfer",
        facts=facts,
    )
    assert dropped == []


def test_sanitize_drops_a_numeric_symbol_suggestion():
    """A stray numeric "symbol" (mistyped amount) must never reach the card."""
    facts = {"supported_chains": ["ARBITRUM"], "supported_assets": [], "balances": []}

    kept = _sanitize_response_suggestions(
        [
            {
                "label": "转 ETH",
                "message": "转 ETH",
                "data": {"intent": "transfer", "chain": "ARBITRUM", "symbol": "ETH"},
            }
        ],
        intent="transfer",
        facts=facts,
    )
    assert [item["data"]["symbol"] for item in kept] == ["ETH"]

    dropped = _sanitize_response_suggestions(
        [
            {
                "label": "转 0",
                "message": "转 0",
                "data": {"intent": "transfer", "chain": "ARBITRUM", "symbol": "0"},
            }
        ],
        intent="transfer",
        facts=facts,
    )
    assert dropped == []


@pytest.mark.asyncio
async def test_amount_card_ignores_a_numeric_symbol_and_asks_for_the_coin():
    """A mistyped "0" symbol must not render an amount card; ask for the coin."""
    graph = build_graph(model=FakeModel())
    result = await graph.ainvoke(
        {
            "request": {"message": "转eth", "address": EVM, "chain": "BASE"},
            "intent": "transfer",
            "transfer_draft": {
                "chain": "BASE",
                "symbol": "0",
                "recipient": "0x" + "2" * 40,
            },
            "wallet_context": {
                "address": EVM,
                "chain": "BASE",
                "wallet_accounts": [{"chain": "BASE", "address": EVM}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "transfer-numeric-symbol"}},
    )
    response = result["response"]
    assert response["missing_fields"] == ["transfer_amount"]
    suggestions = response["suggestions"]
    assert suggestions, "the coin card should still be offered"
    assert all(
        item["type"] != "amount" or item["data"].get("symbol") != "0"
        for item in suggestions
    )
    # The card asks for the coin (native ETH + wallet tokens), not an amount
    # anchored to a "0" coin.
    assert any(item["type"] == "symbol" for item in suggestions)


def _chain_logo(chain: str) -> str:
    return (
        "https://raw.githubusercontent.com/trustwallet/assets/master"
        f"/blockchains/{chain}/info/logo.png"
    )


@pytest.mark.asyncio
async def test_amount_chips_carry_the_native_coin_icon_and_chain_badge():
    """The design's amount card renders the asset icon on its 资产与余额 line."""
    graph = build_graph(model=FakeModel())
    result = await graph.ainvoke(
        {
            "request": {"message": "转 0x…", "address": EVM, "chain": "BASE"},
            "intent": "transfer",
            "transfer_draft": {
                "chain": "BASE",
                "symbol": "ETH",
                "recipient": "0x" + "2" * 40,
            },
            "wallet_context": {
                "address": EVM,
                "chain": "BASE",
                "wallet_accounts": [{"chain": "BASE", "address": EVM}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "transfer-amount-logos"}},
    )
    response = result["response"]
    assert response["kind"] == "clarification"
    assert response["missing_fields"] == ["transfer_amount"]
    amount = response["suggestions"][0]
    assert amount["type"] == "amount"
    # ETH is the chain's native coin, so its icon is the chain logo and must not
    # stay null just because no provider supplied one.
    assert amount["logo_url"] == _chain_logo("base")
    assert amount["chain_logo_url"] == _chain_logo("base")


@pytest.mark.asyncio
async def test_coin_chips_keep_the_wallet_asset_icon():
    """A wallet token's provider logo rides through to the coin chip."""
    logo = "https://static.okx.com/usdc.png"
    graph = build_graph(model=FakeModel())
    result = await graph.ainvoke(
        {
            "request": {"message": "转 0x…", "address": EVM, "chain": "BASE"},
            "intent": "transfer",
            "transfer_draft": {"chain": "BASE", "recipient": "0x" + "2" * 40},
            "wallet_context": {
                "address": EVM,
                "chain": "BASE",
                "wallet_accounts": [{"chain": "BASE", "address": EVM}],
                "token_balances": [
                    {
                        "asset": {
                            "chain": "BASE",
                            "symbol": "USDC",
                            "address": "0x" + "3" * 40,
                            "decimals": 6,
                            "logo_url": logo,
                        }
                    }
                ],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "transfer-coin-logos"}},
    )

    suggestions = {
        item["value"]: item for item in result["response"]["suggestions"]
    }
    assert suggestions["ETH"]["logo_url"] == _chain_logo("base")
    assert suggestions["ETH"]["chain_logo_url"] == _chain_logo("base")
    assert suggestions["USDC"]["logo_url"] == logo
    assert suggestions["USDC"]["chain_logo_url"] == _chain_logo("base")


@pytest.mark.asyncio
async def test_missing_amount_carries_selected_chain_balance_not_fallback():
    """V169-IOS-018: the amount card must anchor 25/50/75/100% to the selected
    chain's balance (ARBITRUM ETH), never fall back to another chain's
    same-symbol balance (mainnet ETH)."""
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance

    class ArbWallet:
        chain_index_by_name = {"ETH": "1", "ARBITRUM": "42161"}

        async def get_token_balances(self, address, indexes):
            assert len(indexes) == 1
            if indexes[0] == "1":
                return [
                    TokenBalance(
                        asset=Asset(chain="ETH", symbol="ETH", decimals=18),
                        amount=Decimal("0.015512539042017515"),
                        amount_raw="15512539042017515000",
                        usd_value=Decimal("40"),
                    )
                ]
            assert indexes[0] == "42161"
            return [
                TokenBalance(
                    asset=Asset(chain="ARBITRUM", symbol="ETH", decimals=18),
                    amount=Decimal("0.000030467544104"),
                    amount_raw="30467544104000",
                    usd_value=Decimal("0.08"),
                )
            ]

    graph = build_graph(model=FakeModel(), wallet_provider=ArbWallet())
    result = await graph.ainvoke(
        {
            "request": {"message": "转 0x…", "address": EVM, "chain": "ARBITRUM"},
            "intent": "transfer",
            "transfer_draft": {
                "chain": "ARBITRUM",
                "symbol": "ETH",
                "recipient": "0x" + "2" * 40,
            },
            "wallet_context": {
                "address": EVM,
                "chain": "ARBITRUM",
                "wallet_accounts": [{"chain": "ARBITRUM", "address": EVM}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "transfer-amount-arb-balance"}},
    )

    response = result["response"]
    assert response["kind"] == "clarification"
    assert response["missing_fields"] == ["transfer_amount"]
    amounts = [item for item in response["suggestions"] if item["type"] == "amount"]
    assert [(item["value"]) for item in amounts] == ["25%", "50%", "75%", "100%"]
    # The card anchors to the ARBITRUM balance, not the mainnet ETH balance.
    assert amounts[0]["data"]["chain"] == "ARBITRUM"
    assert amounts[0]["data"]["symbol"] == "ETH"
    assert amounts[0]["data"]["balance"] == "0.000030467544104"
    assert amounts[0]["data"]["balance_usd"] == "0.08"
