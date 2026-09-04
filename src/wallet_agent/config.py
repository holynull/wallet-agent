"""Application configuration loaded from environment variables and dotenv files."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for provider adapters, the model, and bounded polling."""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bridgers_enabled: bool = True
    bridgers_source_flag: str = ""
    omnibridge_enabled: bool = True
    omnibridge_source_flag: str = ""

    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str | None = None

    provider_timeout_seconds: float = 15
    http_timeout_seconds: float = 15
    allowed_chains: list[str] = Field(default_factory=lambda: ["EVM", "TRON", "SOLANA"])
    rpc_urls: dict[str, str] = Field(default_factory=dict)

    persistence_url: str = "sqlite+aiosqlite:///./wallet_agent.db"
    poll_interval_seconds: float = 5
    poll_max_attempts: int = 20

