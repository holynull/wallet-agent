import pytest
from pydantic import SecretStr, ValidationError

from wallet_agent.config import Settings


def test_okx_is_disabled_without_credentials(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    settings = Settings(_env_file=None)
    assert settings.okx_enabled is False
    assert settings.okx_base_url == "https://web3.okx.com"
    assert settings.okx_max_attempts == 3
    assert settings.okx_exclude_risk_tokens is True


def test_enabled_okx_requires_all_credentials(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("OKX_ENABLED", "true")
    monkeypatch.setenv("OKX_API_KEY", "api-key")
    monkeypatch.setenv("OKX_SECRET_KEY", "secret")
    monkeypatch.setenv("OKX_PASSPHRASE", "passphrase")
    with pytest.raises(ValidationError, match="OKX_PROJECT_ID"):
        Settings(_env_file=None)


def test_enabled_okx_uses_secret_types_and_redacts_repr(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    settings = Settings(
        okx_enabled=True,
        okx_api_key="api-key-value",
        okx_secret_key="secret-value",
        okx_passphrase="passphrase-value",
        okx_project_id="project-value",
    )
    assert isinstance(settings.okx_secret_key, SecretStr)
    assert settings.okx_secret_key.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(settings)
    assert "passphrase-value" not in repr(settings)
