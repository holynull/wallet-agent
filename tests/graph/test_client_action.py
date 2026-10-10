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
async def test_unknown_client_action_is_rejected_without_telling_the_app_to_act():
    result = await _run({"action": "open_calendar"}, "client-action-unknown")

    response = result["response"]
    assert "client_action" not in response
    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages
    assert '"ok": false' in tool_messages[0].content
    assert "CLIENT_ACTION_UNSUPPORTED" in tool_messages[0].content
    assert "open_contacts" in tool_messages[0].content
