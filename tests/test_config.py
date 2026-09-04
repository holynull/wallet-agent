from wallet_agent.config import Settings


def test_settings_load_non_secret_defaults(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    settings = Settings()
    assert settings.provider_timeout_seconds == 15
    assert settings.poll_max_attempts == 20
    assert settings.allowed_chains == ["EVM", "TRON", "SOLANA"]
