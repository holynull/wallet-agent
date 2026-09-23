"""Application configuration loaded from environment variables and dotenv files."""

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for provider adapters, the model, and bounded polling."""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bridgers_enabled: bool = False
    bridgers_base_url: str | None = None
    bridgers_source_flag: str = ""
    bridgers_spender_by_chain: dict[str, str] = Field(default_factory=dict)
    bridgers_swap_spender: str | None = None
    omnibridge_enabled: bool = False
    omnibridge_base_url: str | None = None
    omnibridge_source_flag: str = ""
    omnibridge_spender_by_chain: dict[str, str] = Field(default_factory=dict)
    omnibridge_swap_spender: str | None = None

    # DeepSeek is the default OpenAI-compatible backend. The OPENAI_* names
    # remain supported for backwards compatibility with existing deployments.
    deepseek_api_key: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "deepseek-v4-pro"
    openai_base_url: str | None = "https://api.deepseek.com"
    allowed_model_ids: list[str] = Field(
        default_factory=lambda: ["deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"]
    )

    provider_timeout_seconds: float = Field(default=15, gt=0)
    http_timeout_seconds: float = Field(default=15, gt=0)
    allowed_chains: list[str] = Field(default_factory=lambda: ["EVM", "TRON", "SOLANA"])
    rpc_urls: dict[str, str | list[str]] = Field(default_factory=dict)
    rpc_timeout_seconds: float = Field(default=10, gt=0)
    rpc_max_attempts: int = Field(default=2, ge=1, le=5)

    okx_enabled: bool = False
    okx_base_url: str = "https://web3.okx.com"
    okx_api_key: SecretStr | None = None
    okx_secret_key: SecretStr | None = None
    okx_passphrase: SecretStr | None = None
    okx_project_id: SecretStr | None = None
    okx_max_attempts: int = Field(default=3, ge=1, le=5)
    okx_cache_ttl_seconds: int = Field(default=60, ge=0)
    okx_exclude_risk_tokens: bool = True
    asset_cache_ttl_seconds: int = Field(default=600, ge=0)

    persistence_url: str = "sqlite+aiosqlite:///./wallet_agent.db"
    auth_required: bool = False
    auth_tokens: dict[str, str] = Field(default_factory=dict)
    poll_interval_seconds: float = Field(default=5, gt=0)
    poll_max_attempts: int = Field(default=20, ge=1, le=1000)
    confirmation_ttl_seconds: int = Field(default=900, ge=1, le=86_400)

    @model_validator(mode="after")
    def require_model_key(self) -> "Settings":
        if not (self.deepseek_api_key or self.openai_api_key):
            raise ValueError("DEEPSEEK_API_KEY or OPENAI_API_KEY is required")
        if self.okx_enabled:
            missing = [
                name
                for name, value in (
                    ("OKX_API_KEY", self.okx_api_key),
                    ("OKX_SECRET_KEY", self.okx_secret_key),
                    ("OKX_PASSPHRASE", self.okx_passphrase),
                    ("OKX_PROJECT_ID", self.okx_project_id),
                )
                if value is None or not value.get_secret_value()
            ]
            if missing:
                raise ValueError(f"{', '.join(missing)} required when OKX_ENABLED=true")
        return self
