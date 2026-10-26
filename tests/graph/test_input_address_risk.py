"""Input-time address risk card projection."""

import pytest
from langchain_core.messages import AIMessage

from wallet_agent.graph.build import build_graph

RECIPIENT = "0x1F6E4FD185ce8C8171b4BDd2A64667BC69Aff11A"
WALLET = "0x" + "3" * 40


class ProseModel:
    async def invoke_react(self, messages, tools):
        return AIMessage(content="请确认地址后再继续。")


class ContractAdapter:
    def __init__(self, is_contract):
        self._is_contract = is_contract
        self.checked = []

    async def is_contract(self, address):
        self.checked.append(address)
        return self._is_contract


async def _turn(*, metadata=None, adapter=None):
    graph = build_graph(
        model=ProseModel(),
        chains={"ETH": adapter or object()},
    )
    request = {"message": f"转给 {RECIPIENT}", "address": WALLET, "chain": "ETH"}
    if metadata is not None:
        request["metadata"] = metadata
    result = await graph.ainvoke(
        {
            "request": request,
            "wallet_context": {
                "address": WALLET,
                "chain": "ETH",
                "wallet_accounts": [{"chain": "ETH", "address": WALLET}],
            },
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": f"input-risk-{id(metadata)}-{id(adapter)}"}},
    )
    return result["response"]


@pytest.mark.asyncio
async def test_contract_recipient_opens_the_designed_risk_card():
    adapter = ContractAdapter(True)

    response = await _turn(adapter=adapter)

    action = response["client_action"]
    assert action["type"] == "address_risk"
    slots = action["slots"]
    assert slots["address"] == RECIPIENT
    assert slots["chain"] == "ETH"
    assert slots["level"] == "warning"
    assert slots["code"] == "CONTRACT_ADDRESS_RECIPIENT"
    assert slots["title"] == "地址风险提示"
    assert slots["cancel_label"] == "取消转账"
    assert slots["continue_label"] == "继续转账"
    assert slots["blocking"] is False
    assert adapter.checked == [RECIPIENT]


@pytest.mark.asyncio
async def test_eoa_recipient_does_not_open_a_risk_card():
    adapter = ContractAdapter(False)

    response = await _turn(adapter=adapter)

    assert "client_action" not in response
    assert adapter.checked == [RECIPIENT]


@pytest.mark.asyncio
async def test_client_high_risk_verdict_is_relayed_for_the_same_address():
    response = await _turn(
        metadata={
            "address_risk": {
                "address": RECIPIENT,
                "level": "danger",
                "code": "PHISHING_ADDRESS",
                "message": "该地址命中钓鱼地址库。",
            }
        }
    )

    slots = response["client_action"]["slots"]
    assert slots["level"] == "danger"
    assert slots["code"] == "PHISHING_ADDRESS"
    assert slots["message"] == "该地址命中钓鱼地址库。"
    assert slots["blocking"] is True
    assert slots["source"] == "client_metadata"
