"""Design v1.6.9 receive-address card: the assistant hands back the App's own addresses."""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import _receive_address_payload

EVM = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"
BTC_SEGWIT = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"
BTC_TAPROOT = "bc1p47u7v2k5cpfqt0gxftkat3ade7ucs588g8lr0znzr9le06taty6q6x9gut"
TRON = "TNwXHkvCwj3nayWPbFbxD7rBPncBsyBS27"

CONTEXT = {
    "address": EVM,
    "chain": "ETH",
    "supported_networks": ["ETH", "BSC", "BTC", "TRON"],
    "wallet_accounts": [
        {"chain": "ETH", "address": EVM},
        {"chain": "BSC", "address": EVM},
        {"chain": "BTC", "address": BTC_SEGWIT},
        {"chain": "BTC", "address": BTC_TAPROOT},
        {"chain": "TRON", "address": TRON},
    ],
}


def test_payload_collapses_evm_and_lists_every_supported_chain():
    payload = _receive_address_payload(CONTEXT)

    assert payload["ok"] is True
    assert payload["kind"] == "receive_address"
    assert payload["address"] == EVM
    assert [group["group"] for group in payload["groups"]] == ["EVM", "BTC", "TRON"]

    evm = payload["groups"][0]
    assert evm["label"] == "支持所有 EVM 链"
    assert evm["chains"] == ["ETH", "BSC"]
    assert payload["groups"][1]["addresses"] == [BTC_SEGWIT, BTC_TAPROOT]
    assert payload["supported_chains"] == ["ETH", "BSC", "BTC", "TRON"]
    assert "Ethereum / BNB Chain" in payload["message"]


def test_payload_merges_secondary_evm_chains_into_the_evm_group():
    """AVAXC/ROBINHOOD/MEGAETH/CELO/ARC share the EVM address and must not end
    up as separate groups."""
    context = {
        **CONTEXT,
        "supported_networks": ["ETH", "AVAXC", "CELO", "BTC", "TRON"],
        "wallet_accounts": [
            {"chain": "ETH", "address": EVM},
            {"chain": "AVAXC", "address": EVM},
            {"chain": "CELO", "address": EVM},
            {"chain": "BTC", "address": BTC_SEGWIT},
            {"chain": "TRON", "address": TRON},
        ],
    }

    payload = _receive_address_payload(context)

    assert [group["group"] for group in payload["groups"]] == ["EVM", "BTC", "TRON"]
    evm = payload["groups"][0]
    assert set(evm["chains"]) == {"ETH", "AVAXC", "CELO"}
    assert evm["addresses"] == [EVM]


def test_payload_falls_back_to_the_single_connected_account():
    payload = _receive_address_payload({"chain": "TRON", "address": TRON})

    assert payload["address"] == TRON
    assert payload["supported_chains"] == ["TRON"]
    assert payload["groups"][0]["label"] == "TRON"


def test_payload_exposes_a_badge_for_every_supported_chain():
    """The card's chain chips are text today; the lookup lets the App badge them."""
    payload = _receive_address_payload(CONTEXT)

    logos = payload["chain_logo_urls"]
    assert set(logos) == {"ETH", "BSC", "BTC", "TRON"}
    assert logos["ETH"].endswith("/blockchains/ethereum/info/logo.png")
    assert logos["BSC"].endswith("/blockchains/binance/info/logo.png")
    assert logos["BTC"].endswith("/blockchains/bitcoin/info/logo.png")
    assert logos["TRON"].endswith("/blockchains/tron/info/logo.png")


def test_payload_reports_when_no_address_is_available():
    payload = _receive_address_payload({"chain": "ETH", "address": "not-an-address"})

    assert payload["ok"] is False
    assert payload["code"] == "RECEIVE_ADDRESS_UNAVAILABLE"
    assert payload["address"] is None


class ReceiveModel:
    """Calls receive_address once, then answers from the tool observation."""

    async def invoke_react(self, messages, tools):
        assert "receive_address" in [tool.name for tool in tools]
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[{"id": "recv-1", "name": "receive_address", "args": {}}],
            )
        return AIMessage(content="这是你的收款地址 👆")


@pytest.mark.asyncio
async def test_graph_returns_the_receive_address_payload():
    graph = build_graph(model=ReceiveModel())

    result = await graph.ainvoke(
        {
            "request": {"message": "获取我的收款地址", "address": EVM, "chain": "ETH"},
            "wallet_context": CONTEXT,
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "receive-address"}},
    )

    assert result["response"]["kind"] == "react"
    assert result["response"]["message"] == "这是你的收款地址 👆"
    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages, "the tool must run"
    assert EVM in tool_messages[0].content
    assert "支持所有 EVM 链" in tool_messages[0].content
