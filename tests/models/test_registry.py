import pytest

from wallet_agent.models import ModelRegistry


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
