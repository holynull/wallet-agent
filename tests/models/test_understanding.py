import pytest
from langchain_core.messages import HumanMessage

from wallet_agent.models import (
    ModelRegistry,
    ModelRouter,
    RouteDecision,
    SwapSlotPatch,
    TransferSlotPatch,
)


class CapturingClient:
    def __init__(self, output):
        self.output = output
        self.inputs = []

    async def ainvoke(self, value):
        self.inputs.append(value)
        return self.output


def router_with_clients(*, classifier=None, transfer=None, swap=None):
    classifier = classifier or CapturingClient(RouteDecision(intent="clarification"))
    return ModelRouter(
        ModelRegistry(
            {"deepseek-chat": classifier},
            default_model_id="deepseek-chat",
            extractors={
                "transfer": {"deepseek-chat": transfer or CapturingClient(TransferSlotPatch())},
                "swap": {"deepseek-chat": swap or CapturingClient(SwapSlotPatch())},
            },
        )
    )


def test_route_decision_rejects_transaction_slot_fields():
    with pytest.raises(ValueError):
        RouteDecision(intent="transfer", transfer_amount="1")


def test_route_decision_accepts_deepseek_json_object_marker():
    decision = RouteDecision.model_validate(
        {"type": "json_object", "intent": "wallet_query"}
    )

    assert decision.intent == "wallet_query"
    assert decision.model_dump() == {"intent": "wallet_query"}


@pytest.mark.asyncio
async def test_transfer_extractor_names_every_slot_and_preserves_values():
    recipient = "0x" + "2" * 40
    client = CapturingClient(
        TransferSlotPatch(chain="Base", symbol="ETH", amount="0.01", recipient=recipient)
    )
    router = router_with_clients(transfer=client)

    patch = await router.extract(
        "transfer",
        {"message": f"在 Base 给 {recipient} 转 0.01 ETH"},
    )

    assert patch.amount == "0.01"
    assert patch.recipient == recipient
    assert isinstance(client.inputs[0][0], HumanMessage)
    prompt = client.inputs[0][0].content
    assert all(
        field in prompt
        for field in ("chain", "symbol", "token_address", "decimals", "amount", "recipient")
    )
    assert "参数不完整时仍然是 transfer" in prompt
    assert "不要猜测" in prompt


@pytest.mark.asyncio
async def test_swap_extractor_receives_existing_slots_and_explicit_schema():
    client = CapturingClient(SwapSlotPatch(destination_chain="BASE"))
    router = router_with_clients(swap=client)

    patch = await router.extract(
        "swap",
        {
            "message": "都在 Base 链",
            "active_task": {
                "kind": "swap",
                "slots": {"source_symbol": "USDC", "destination_symbol": "USDT"},
            },
        },
    )

    assert patch.destination_chain == "BASE"
    prompt = client.inputs[0][0].content
    assert all(
        field in prompt
        for field in (
            "source_chain",
            "destination_chain",
            "source_symbol",
            "destination_symbol",
            "source_token_address",
            "destination_token_address",
            "input_amount",
        )
    )
    assert "参数不完整时仍然是 swap" in prompt
    assert '"source_symbol": "USDC"' in prompt


@pytest.mark.asyncio
async def test_ainvoke_remains_a_classification_compatibility_alias():
    client = CapturingClient(RouteDecision(intent="wallet_query"))
    router = router_with_clients(classifier=client)

    result = await router.ainvoke({"message": "查询余额"})

    assert result == {"intent": "wallet_query"}
    assert isinstance(client.inputs[0][0], HumanMessage)
