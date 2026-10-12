import pytest
from pydantic import ValidationError

from wallet_agent.config import Settings


def test_settings_load_non_secret_defaults(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    settings = Settings(_env_file=None)
    assert settings.bridgers_enabled is False
    assert settings.omnibridge_enabled is False
    assert settings.provider_timeout_seconds == 15
    assert settings.poll_max_attempts == 20
    assert settings.allowed_chains == ["EVM", "BTC", "TRON", "SOLANA"]
    assert settings.openai_model == "deepseek-flash"
    assert settings.intent_model == "deepseek-flash"
    assert settings.slot_extraction_model == "deepseek-flash"
    assert settings.response_model == "deepseek-flash"
    assert settings.read_agent_enabled is True
    assert settings.read_agent_model == "deepseek-flash"
    assert settings.deepseek_thinking_enabled is False
    assert settings.deepseek_reasoning_effort == "none"
    assert settings.read_agent_thinking_enabled is True
    assert settings.read_agent_reasoning_effort == "low"
    assert settings.react_compatibility_mode is False
    assert settings.react_max_steps == 8
    assert settings.react_tool_timeout_seconds == 20
    assert settings.allowed_model_ids == ["deepseek-flash", "deepseek-v4-pro"]
    assert settings.metadata_cache_ttl_seconds == 600
    assert settings.metadata_cache_max_entries == 512
    assert settings.openai_base_url == "https://api.deepseek.com"
    assert settings.redis_url is None
    assert settings.redis_key_prefix == "wallet-agent"
    assert settings.run_event_ttl_seconds == 86_400
    assert settings.run_event_max_entries == 2_000
    assert settings.conversation_lock_lease_seconds == 120
    assert settings.conversation_lock_wait_seconds == 30


def test_react_compatibility_mode_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    settings = Settings(_env_file=None, react_compatibility_mode=True)

    assert settings.react_compatibility_mode is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_timeout_seconds", 0),
        ("http_timeout_seconds", -1),
        ("poll_interval_seconds", 0),
        ("poll_max_attempts", 0),
        ("poll_max_attempts", 1001),
        ("run_event_ttl_seconds", 59),
        ("run_event_max_entries", 99),
        ("conversation_lock_lease_seconds", 1),
        ("conversation_lock_wait_seconds", 0),
        ("react_max_steps", 0),
        ("react_max_steps", 33),
        ("react_tool_timeout_seconds", 0),
        ("metadata_cache_max_entries", -1),
    ],
)
def test_settings_rejects_invalid_polling_and_timeout_values(monkeypatch, field, value):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


