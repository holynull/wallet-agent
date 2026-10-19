import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from wallet_agent.models import (
    AgentResponseDraft,
    ModelRegistry,
    ModelRouter,
    PriceSlotPatch,
    RouteDecision,
    SwapSlotPatch,
    TransactionStatusSlotPatch,
    TransferSlotPatch,
)


class CapturingClient:
    def __init__(self, output):
        self.output = output
        self.inputs = []

    async def ainvoke(self, value):
        self.inputs.append(value)
        return self.output


def router_with_clients(
    *, classifier=None, transfer=None, swap=None, transaction_status=None, price=None
):
    classifier = classifier or CapturingClient(RouteDecision(intent="clarification"))
    return ModelRouter(
        ModelRegistry(
            {"deepseek-chat": classifier},
            default_model_id="deepseek-chat",
            extractors={
                "transfer": {"deepseek-chat": transfer or CapturingClient(TransferSlotPatch())},
                "swap": {"deepseek-chat": swap or CapturingClient(SwapSlotPatch())},
                "transaction_status": {"deepseek-chat": transaction_status or CapturingClient({})},
                "price": {"deepseek-chat": price or CapturingClient(PriceSlotPatch())},
            },
        )
    )


def test_route_decision_rejects_transaction_slot_fields():
    with pytest.raises(ValueError):
        RouteDecision(intent="transfer", transfer_amount="1")


def test_route_decision_accepts_deepseek_json_object_marker():
    decision = RouteDecision.model_validate({"type": "json_object", "intent": "wallet_query"})

    assert decision.intent == "wallet_query"
    assert decision.model_dump() == {"intent": "wallet_query"}


def test_response_draft_accepts_merged_json_object_envelope():
    """DeepSeek sometimes merges the draft into {type, message, suggestions}."""
    draft = AgentResponseDraft.model_validate(
        {
            "type": "json_object",
            "message": "这笔交易已确认：转出 10 USDT。",
            "suggestions": [
                {
                    "label": "查看详情",
                    "message": "查看详情",
                    "data": {"intent": "transaction_status"},
                }
            ],
        }
    )

    assert draft.language == "zh"
    assert draft.message == "这笔交易已确认：转出 10 USDT。"
    assert draft.suggestions[0].label == "查看详情"


def test_response_draft_accepts_nested_content_envelope_and_preserves_language():
    draft = AgentResponseDraft.model_validate(
        {
            "type": "json_object",
            "content": {
                "reply": "你的钱包在 ETH 链上余额为 1.5 ETH。",
                "suggestions": [
                    {"label": "查看价格", "message": "查看价格", "data": {"chain": "ETH"}}
                ],
            },
        }
    )

    assert draft.language == "zh"
    assert draft.message == "你的钱包在 ETH 链上余额为 1.5 ETH。"
    assert draft.suggestions[0].label == "查看价格"


def test_response_draft_accepts_plain_content_string_and_defaults_language():
    draft = AgentResponseDraft.model_validate(
        {"type": "response", "content": "交易已提交，正在等待确认。"}
    )
    assert draft.language == "zh"
    assert draft.message == "交易已提交，正在等待确认。"

    english = AgentResponseDraft.model_validate(
        {"type": "json_object", "content": "Your transaction is confirmed."}
    )
    assert english.language == "en"
    assert english.message == "Your transaction is confirmed."


def test_slot_patches_strip_json_object_marker():
    patch = TransactionStatusSlotPatch.model_validate(
        {"type": "json_object", "transaction_chain": "ETH", "transaction_hash": "0x" + "a" * 64}
    )
    assert patch.transaction_chain == "ETH"
    assert patch.transaction_hash == "0x" + "a" * 64


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
    assert isinstance(client.inputs[0][0], SystemMessage)
    assert isinstance(client.inputs[0][1], HumanMessage)
    prompt = "\n".join(item.content for item in client.inputs[0])
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
    prompt = "\n".join(item.content for item in client.inputs[0])
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
    assert "‘换一些 USDT’" in prompt
    assert "destination_symbol，不是 source_symbol" in prompt


@pytest.mark.asyncio
async def test_swap_extractor_prompt_includes_clicked_structured_suggestion():
    client = CapturingClient(SwapSlotPatch(source_chain="ETH", destination_chain="ETH"))
    router = router_with_clients(swap=client)

    await router.extract(
        "swap",
        {
            "message": "都在 ETH 链",
            "suggestion_data": {
                "source_symbol": "ETH",
                "destination_symbol": "USDT",
                "output_amount": "10",
                "amount_mode": "exact_out",
            },
        },
    )

    prompt = "\n".join(item.content for item in client.inputs[0])
    assert "suggestion_data" in prompt
    assert '"output_amount": "10"' in prompt


@pytest.mark.asyncio
async def test_transaction_status_extractor_preserves_chain_and_hash():
    tx_hash = "0x" + "a" * 64
    client = CapturingClient({"transaction_chain": "ETH", "transaction_hash": tx_hash})
    router = router_with_clients(transaction_status=client)

    patch = await router.extract(
        "transaction_status",
        {"message": f"以太，{tx_hash}"},
    )

    assert patch.transaction_chain == "ETH"
    assert patch.transaction_hash == tx_hash
    prompt = "\n".join(item.content for item in client.inputs[0])
    assert "transaction_chain" in prompt
    assert "transaction_hash" in prompt


@pytest.mark.asyncio
async def test_price_extractor_recognizes_asset_from_natural_language():
    client = CapturingClient(PriceSlotPatch(symbol="BTC"))
    router = router_with_clients(price=client)

    patch = await router.extract("price", {"message": "今天 BTC 的价格是多少"})

    assert patch.symbol == "BTC"
    assert patch.chain is None
    assert "symbol" in client.inputs[0][0].content


@pytest.mark.asyncio
async def test_ainvoke_remains_a_classification_compatibility_alias():
    client = CapturingClient(RouteDecision(intent="wallet_query"))
    router = router_with_clients(classifier=client)

    result = await router.ainvoke({"message": "查询余额"})

    assert result == {"intent": "wallet_query"}
    assert isinstance(client.inputs[0][0], SystemMessage)
    assert isinstance(client.inputs[0][1], HumanMessage)


def test_read_only_price_schema_prefers_server_asset_resolution():
    from wallet_agent.graph.read_only import READ_ONLY_TOOL_SCHEMAS

    price_lookup = next(
        item["function"] for item in READ_ONLY_TOOL_SCHEMAS
        if item["function"]["name"] == "price_lookup"
    )
    description = price_lookup["description"].lower()
    assert "resolve" in description
    assert "decimals" in description
    assert "user" in description


def test_swap_slot_patch_accepts_numeric_usd_target_from_json_models():
    patch = SwapSlotPatch.model_validate(
        {"target_value_amount": 10, "target_value_currency": "USD"}
    )

    assert patch.target_value_amount == "10"
    assert patch.target_value_currency == "USD"
