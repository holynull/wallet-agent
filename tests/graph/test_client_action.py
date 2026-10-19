"""The agent asks the App to open its own surfaces instead of collecting that data."""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.graph.build import build_graph

EVM = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"
CONTEXT = {
    "address": EVM,
    "chain": "BASE",
    "wallet_accounts": [{"chain": "BASE", "address": EVM}],
}


class ClientActionModel:
    def __init__(self, args):
        self.args = args

    async def invoke_react(self, messages, tools):
        assert "request_client_action" in [tool.name for tool in tools]
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[{"id": "ca-1", "name": "request_client_action", "args": self.args}],
            )
        return AIMessage(content="请从通讯录里选一个联系人 👇")


async def _run(args, thread_id):
    graph = build_graph(model=ClientActionModel(args))
    result = await graph.ainvoke(
        {
            "request": {"message": "转一笔给朋友", "address": EVM, "chain": "BASE"},
            "wallet_context": CONTEXT,
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    return result


@pytest.mark.asyncio
async def test_contacts_request_reaches_the_app_as_a_client_action():
    result = await _run(
        {"action": "open_contacts", "reason": "transfer_recipient", "chain": "BASE"},
        "client-action-contacts",
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert response["client_action"] == {
        "type": "open_contacts",
        "reason": "transfer_recipient",
        "slots": {"chain": "BASE"},
    }
    # The tool result stays available for the App's audit trail.
    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages and "open_contacts" in tool_messages[0].content


@pytest.mark.asyncio
async def test_address_input_request_reaches_the_app_as_a_client_action():
    result = await _run(
        {"action": "address_input", "reason": "transfer_recipient", "chain": "BASE"},
        "client-action-address-input",
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert response["client_action"] == {
        "type": "address_input",
        "reason": "transfer_recipient",
        "slots": {"chain": "BASE"},
    }
    # The tool result stays available for the App's audit trail.
    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages and "address_input" in tool_messages[0].content


@pytest.mark.asyncio
async def test_unknown_client_action_is_rejected_without_telling_the_app_to_act():
    result = await _run({"action": "open_calendar"}, "client-action-unknown")

    response = result["response"]
    assert "client_action" not in response
    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages
    assert '"ok": false' in tool_messages[0].content
    assert "CLIENT_ACTION_UNSUPPORTED" in tool_messages[0].content
    assert "open_contacts" in tool_messages[0].content


@pytest.mark.asyncio
async def test_amount_keypad_turn_does_not_project_a_portfolio_card():
    """A transfer amount step (open_amount_keypad) must not also render an
    asset-overview card from the same round's wallet_balance observation."""
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance
    from wallet_agent.graph.build import build_graph

    class KeypadWalletProvider:
        chain_index_by_name = {"BSC": "56"}

        async def get_token_balances(self, address, indexes):
            assert indexes == ["56"]
            return [
                TokenBalance(
                    asset=Asset(chain="BSC", chain_id=56, symbol="USDT", decimals=18),
                    amount=Decimal("11.72"),
                    amount_raw="11720000000000000000",
                    usd_value=Decimal("11.72"),
                )
            ]

    class KeypadWithBalanceModel:
        async def invoke_react(self, messages, tools):
            tool_messages = [
                item for item in messages if isinstance(item, ToolMessage)
            ]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "wb-1",
                            "name": "wallet_balance",
                            "args": {"chain": "BSC", "symbol": "USDT"},
                        },
                        {
                            "id": "ca-1",
                            "name": "request_client_action",
                            "args": {
                                "action": "open_amount_keypad",
                                "chain": "BSC",
                                "symbol": "USDT",
                                "reason": "输入 BSC 网络 USDT 的转账金额",
                            },
                        },
                    ],
                )
            return AIMessage(content="你 BSC 上的 USDT 余额约 11.72，请输入要转账的金额。")

    graph = build_graph(
        model=KeypadWithBalanceModel(),
        wallet_provider=KeypadWalletProvider(),
    )
    result = await graph.ainvoke(
        {
            "request": {
                "message": "转 USDT",
                "address": EVM,
                "chain": "BSC",
                "metadata": {
                    "suggestion_data": {"intent": "transfer", "chain": "BSC", "symbol": "USDT"}
                },
            },
            "wallet_context": {
                "address": EVM,
                "chain": "BSC",
                "wallet_accounts": [{"chain": "BSC", "address": EVM}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "client-action-keypad-portfolio"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert response["client_action"]["type"] == "open_amount_keypad"
    assert response["client_action"]["slots"]["chain"] == "BSC"
    assert response["client_action"]["slots"]["symbol"] == "USDT"
    # The same round's wallet_balance observation stays in react_results for
    # the audit trail but is never elevated to a top-level portfolio card.
    assert "portfolio" not in response
    assert "wallet_balance" in [item["tool"] for item in response["react_results"]]
