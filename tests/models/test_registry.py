import pytest
from langchain_core.messages import AIMessage, HumanMessage

from wallet_agent.graph.read_only import READ_ONLY_TOOL_SCHEMAS
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

    trace = router.consume_model_trace()
    assert [(item["role"], item["model_id"], item.get("task_kind")) for item in trace] == [
        ("classifier", "deepseek-chat", None),
        ("extractor", "deepseek-chat", "transfer"),
        ("response", "deepseek-chat", None),
        ("read_agent", "deepseek-chat", None),
    ]


@pytest.mark.asyncio
async def test_model_router_trace_includes_call_timing_and_stage():
    class TimedModel:
        async def ainvoke(self, _value):
            return {"intent": "clarification"}

    router = ModelRouter(
        ModelRegistry(
            {"deepseek-chat": TimedModel()}, default_model_id="deepseek-chat"
        )
    )

    await router.classify({"message": "你好"})

    trace = router.consume_model_trace()
    assert len(trace) == 1
    item = trace[0]
    assert item["role"] == "classifier"
    assert item["model_id"] == "deepseek-chat"
    assert item["stage"] == "请求识别"
    assert item["call_id"].startswith("classifier-")
    assert item["timestamp"].endswith("Z")
    assert isinstance(item["elapsed_ms"], int)
    assert item["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_native_model_trace_normalizes_provider_usage_and_finish_reason():
    class BoundModel:
        async def ainvoke(self, _messages):
            return AIMessage(
                content="",
                tool_calls=[],
                response_metadata={
                    "finish_reason": "stop",
                    "token_usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 9,
                        "total_tokens": 129,
                        "prompt_tokens_details": {"cached_tokens": 80},
                    },
                },
            )

    class RawModel:
        def bind_tools(self, _tools):
            return BoundModel()

    router = ModelRouter(
        ModelRegistry(
            {"deepseek-flash": FakeModel()},
            default_model_id="deepseek-flash",
            raw_models={"deepseek-flash": RawModel()},
        )
    )
    await router.invoke_read_tools([HumanMessage(content="ETH price")], READ_ONLY_TOOL_SCHEMAS)

    item = router.consume_model_trace()[0]
    assert item["finish_reason"] == "stop"
    assert item["prompt_tokens"] == 120
    assert item["completion_tokens"] == 9
    assert item["total_tokens"] == 129
    assert item["cached_tokens"] == 80


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
    assert captured["value"][0].type == "system"
    assert isinstance(captured["value"][1], HumanMessage)
    assert "你好" in captured["value"][1].content


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
            assert "read_facts" in value[1].content
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
async def test_model_router_invokes_native_read_tool_model_and_records_tool_call_stage():
    calls = []

    class BoundModel:
        async def ainvoke(self, messages):
            calls.append(messages)
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-price-1",
                        "name": "price_lookup",
                        "args": {"symbol": "ETH"},
                    }
                ],
            )

    class RawModel:
        def bind_tools(self, tools, **kwargs):
            calls.append((tools, kwargs))
            return BoundModel()

    router = ModelRouter(
        ModelRegistry(
            {"deepseek-flash": FakeModel()},
            default_model_id="deepseek-flash",
            raw_models={"deepseek-flash": RawModel()},
        )
    )
    result = await router.invoke_read_tools(
        [HumanMessage(content="ETH price")], READ_ONLY_TOOL_SCHEMAS
    )

    assert result.tool_calls[0]["id"] == "call-price-1"
    assert calls[0] == (
        READ_ONLY_TOOL_SCHEMAS,
        {"tool_choice": "auto", "parallel_tool_calls": True},
    )
    trace = router.consume_model_trace()
    assert trace[0]["stage"] == "只读工具调用"
    assert trace[0]["tool_call_ids"] == ["call-price-1"]


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
    prompt = "\n".join(item.content for item in captured["value"])
    assert "supported_chains" in prompt
    assert "balances" in prompt
    assert "prices" in prompt
    assert "不执行查询" in prompt


@pytest.mark.asyncio
async def test_model_router_response_preserves_react_tool_transcript():
    """Removing the transcript argument would hide tool observations from final generation."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    captured = {}

    class ResponseModel:
        async def ainvoke(self, value):
            captured["value"] = value
            return {"language": "zh", "message": "ETH 当前约 1800 美元。", "suggestions": []}

    registry = ModelRegistry(
        {"deepseek-chat": FakeModel()},
        default_model_id="deepseek-chat",
        responses={"deepseek-chat": ResponseModel()},
    )
    transcript = [
        HumanMessage(content="ETH 现在多少钱"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "price-1", "name": "price_lookup", "args": {"symbol": "ETH"}}
            ],
        ),
        ToolMessage(
            content='{"ok":true,"prices":[{"symbol":"ETH","usd_price":"1800"}]}',
            tool_call_id="price-1",
            name="price_lookup",
        ),
    ]

    router = ModelRouter(registry)
    await router.respond(
        {"message": "ETH 现在多少钱"},
        {"prices": [{"symbol": "ETH", "usd_price": "1800"}]},
        messages=transcript,
    )

    values = captured["value"]
    assert [type(item).__name__ for item in values[1:4]] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
    ]
    assert values[2].tool_calls[0]["id"] == "price-1"
    assert "request/facts" in values[-1].content
    trace = router.consume_model_trace()
    assert trace[0]["role"] == "response"
    assert trace[0]["stage"] == "生成回复"


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
