import pytest
from pydantic import ValidationError

from wallet_agent.config import Settings


def test_settings_load_non_secret_defaults(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    settings = Settings()
    assert settings.bridgers_enabled is False
    assert settings.omnibridge_enabled is False
    assert settings.provider_timeout_seconds == 15
    assert settings.poll_max_attempts == 20
    assert settings.allowed_chains == ["EVM", "TRON", "SOLANA"]
    assert settings.openai_model == "deepseek-chat"
    assert settings.openai_base_url == "https://api.deepseek.com"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_timeout_seconds", 0),
        ("http_timeout_seconds", -1),
        ("poll_interval_seconds", 0),
        ("poll_max_attempts", 0),
        ("poll_max_attempts", 1001),
    ],
)
def test_settings_rejects_invalid_polling_and_timeout_values(monkeypatch, field, value):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    with pytest.raises(ValidationError):
        Settings(**{field: value})
