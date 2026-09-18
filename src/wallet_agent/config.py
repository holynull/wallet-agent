"""Application configuration loaded from environment variables and dotenv files."""

from pydantic import Field, model_validator
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

    coingecko_api_key: str | None = None
    coingecko_base_url: str = "https://pro-api.coingecko.com/api/v3"
    coingecko_token_ids: dict[str, str] = Field(default_factory=dict)
    coingecko_native_ids: dict[str, str] = Field(default_factory=dict)
    price_cache_ttl_seconds: int = Field(default=60, ge=0)

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
        return self
