"""Design v1.6.9 transfer card: quick replies carry an explicit type and value."""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import _sanitize_response_suggestions

EVM = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"


class FakeModel:
    async def ainvoke(self, value):
        return {"intent": value.get("intent", "clarification")}


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
