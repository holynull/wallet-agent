import pytest
from langchain_core.messages import HumanMessage

from wallet_agent.models import ModelRegistry, ModelRouter
from wallet_agent.models.contracts import AgentResponseDraft


class FakeModel:
    pass


def test_model_registry_exposes_role_model_ids():
    registry = ModelRegistry(
        {"deepseek-v4-pro": FakeModel(), "deepseek-chat": FakeModel()},
        default_model_id="deepseek-v4-pro",
        classifier_model_id="deepseek-chat",
        extractor_model_id="deepseek-chat",
        response_model_id="deepseek-v4-pro",
        read_agent_model_id="deepseek-chat",
    )

    assert registry.role_model_ids == {
        "classifier": "deepseek-chat",
        "extractor": "deepseek-chat",
        "response": "deepseek-v4-pro",
        "read_agent": "deepseek-chat",
    }


@pytest.mark.asyncio
async def test_model_router_records_models_used_by_each_call():
    class RoleModel:
        def __init__(self, result):
            self.result = result

        async def ainvoke(self, _value):
            return self.result

    route_model = RoleModel({"intent": "clarification"})
    slot_model = RoleModel({})
    response_model = RoleModel({"language": "zh", "message": "你好", "suggestions": []})
    reader_model = RoleModel({"name": None, "arguments": {}})

    router = ModelRouter(
        ModelRegistry(
            {"deepseek-chat": route_model},
            default_model_id="deepseek-chat",
            extractors={"transfer": {"deepseek-chat": slot_model}},
            responses={"deepseek-chat": response_model},
            readers={"deepseek-chat": reader_model},
        )
    )
    await router.classify({"message": "你好"})
    await router.extract("transfer", {"message": "转账"})
    await router.respond({"message": "你好"}, {})
    await router.plan_read({"message": "余额", "read_facts": {}})

    assert router.consume_model_trace() == [
        {"role": "classifier", "model_id": "deepseek-chat"},
        {"role": "extractor", "model_id": "deepseek-chat", "task_kind": "transfer"},
        {"role": "response", "model_id": "deepseek-chat"},
        {"role": "read_agent", "model_id": "deepseek-chat"},
    ]


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
async def test_model_router_plans_only_whitelisted_read_tools():
    class ReadModel:
        async def ainvoke(self, value):
            assert "read_facts" in value[0].content
            return {"name": "price_lookup", "arguments": {"symbol": "ETH"}}

    router = ModelRouter(
        ModelRegistry(
            {"deepseek-chat": FakeModel()},
            default_model_id="deepseek-chat",
            read_agent_model_id="deepseek-chat",
            readers={"deepseek-chat": ReadModel()},
        )
    )
    result = await router.plan_read({"message": "ETH price", "read_facts": {}})

    assert result.name == "price_lookup"
    assert result.arguments == {"symbol": "ETH"}


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


@pytest.mark.asyncio
async def test_model_router_uses_role_defaults_and_honors_explicit_override():
    calls = []

    class RoleModel:
        def __init__(self, name, result):
            self.name = name
            self.result = result

        async def ainvoke(self, _value):
            calls.append(self.name)
            return self.result

    registry = ModelRegistry(
        {
            "deepseek-v4-pro": RoleModel("pro", {"intent": "clarification"}),
            "deepseek-chat": RoleModel("fast-classifier", {"intent": "swap_quote"}),
        },
        default_model_id="deepseek-v4-pro",
        classifier_model_id="deepseek-chat",
        extractor_model_id="deepseek-chat",
        response_model_id="deepseek-chat",
        extractors={
            "swap": {
                "deepseek-chat": RoleModel("fast-extractor", {"source_symbol": "BNB"}),
                "deepseek-v4-pro": RoleModel("pro-extractor", {"source_symbol": "ETH"}),
            }
        },
        responses={
            "deepseek-chat": RoleModel(
                "fast-response", {"language": "zh", "message": "请补充数量", "suggestions": []}
            ),
            "deepseek-v4-pro": RoleModel(
                "pro-response", {"language": "zh", "message": "复杂回复", "suggestions": []}
            ),
        },
    )
    router = ModelRouter(registry)

    await router.classify({"message": "兑换"})
    await router.extract("swap", {"message": "用 BNB 换"})
    await router.respond({"message": "你好"}, {})
    await router.classify({"model_id": "deepseek-v4-pro", "message": "复杂请求"})

    assert calls == ["fast-classifier", "fast-extractor", "fast-response", "pro"]
