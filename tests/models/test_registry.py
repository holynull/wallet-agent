import pytest
from langchain_core.messages import HumanMessage

from wallet_agent.models import ModelRegistry, ModelRouter
from wallet_agent.models.contracts import AgentResponseDraft


class FakeModel:
    pass


def test_registry_has_default_and_allows_whitelisted_temporary_selection():
    default = FakeModel()
    reasoner = FakeModel()
    registry = ModelRegistry(
        {"deepseek-chat": default, "deepseek-reasoner": reasoner},
        default_model_id="deepseek-chat",
    )
    assert registry.get() is default
    assert registry.validate("deepseek-reasoner") == "deepseek-reasoner"
    assert registry.get("deepseek-reasoner") is reasoner


def test_registry_rejects_unknown_model():
    registry = ModelRegistry({"deepseek-chat": FakeModel()}, default_model_id="deepseek-chat")
    with pytest.raises(ValueError, match="unknown model_id"):
        registry.validate("arbitrary-provider-model")


@pytest.mark.asyncio
async def test_model_router_converts_mapping_request_to_langchain_messages():
    captured = {}

    class CapturingModel:
        async def ainvoke(self, value):
            captured["value"] = value
            return {"intent": "clarification"}

    router = ModelRouter(
        ModelRegistry({"deepseek-chat": CapturingModel()}, default_model_id="deepseek-chat")
    )
    result = await router.ainvoke({"model_id": "deepseek-chat", "message": "你好"})

    assert result == {"intent": "clarification"}
    assert isinstance(captured["value"], list)
    assert isinstance(captured["value"][0], HumanMessage)
    assert "你好" in captured["value"][0].content


def test_agent_response_draft_supports_structured_suggestions():
    response = AgentResponseDraft.model_validate(
        {
            "language": "en",
            "message": "What would you like to swap?",
            "suggestions": [
                {
                    "label": "Swap USDC to ETH",
                    "message": "Swap 10 USDC to ETH",
                    "data": {
                        "intent": "swap_quote",
                        "source_chain": "ETH",
                        "destination_chain": "ETH",
                        "source_symbol": "USDC",
                        "destination_symbol": "ETH",
                        "input_amount": "10",
                        "amount_mode": "exact_in",
                    },
                }
            ],
        }
    )

    assert response.language == "en"
    assert response.suggestions[0].data["input_amount"] == "10"


@pytest.mark.asyncio
async def test_model_router_responds_with_facts_and_structured_contract():
    captured = {}

    class ResponseModel:
        async def ainvoke(self, value):
            captured["value"] = value
            return {
                "language": "en",
                "message": "What would you like to swap?",
                "suggestions": [],
            }

    registry = ModelRegistry(
        {"deepseek-chat": FakeModel()},
        default_model_id="deepseek-chat",
        responses={"deepseek-chat": ResponseModel()},
    )
    result = await ModelRouter(registry).respond(
        {"model_id": "deepseek-chat", "message": "I want to swap"},
        {
            "supported_chains": ["ETH"],
            "balances": [{"symbol": "USDC", "amount": "12"}],
            "prices": [{"symbol": "USDC", "usd_price": "1"}],
        },
    )

    assert isinstance(result, AgentResponseDraft)
    prompt = captured["value"][0].content
    assert "supported_chains" in prompt
    assert "balances" in prompt
    assert "prices" in prompt
    assert "不执行查询" in prompt
