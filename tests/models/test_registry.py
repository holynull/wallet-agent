import pytest
from langchain_core.messages import HumanMessage

from wallet_agent.models import ModelRegistry, ModelRouter


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
