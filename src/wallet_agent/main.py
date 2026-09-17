"""Production application factory.

Provider and model clients are created only when explicitly configured. The
module remains importable in unit-test and documentation environments without
credentials or network access.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from wallet_agent.api import StaticTokenVerifier, create_app
from wallet_agent.chains import build_default_registry
from wallet_agent.config import Settings
from wallet_agent.graph import build_graph
from wallet_agent.models import ModelRegistry, ModelRouter
from wallet_agent.persistence import SqliteSessionStore, initialize_checkpointer
from wallet_agent.prices import CoinGeckoPriceProvider
from wallet_agent.providers import BridgersProvider, HttpJsonTransport, OmniBridgeProvider


class IntentOutput(BaseModel):
    intent: str
    source_chain: str | None = None
    source_chain_id: int | str | None = None
    destination_chain: str | None = None
    destination_chain_id: int | str | None = None
    source_symbol: str | None = None
    destination_symbol: str | None = None
    source_token_address: str | None = None
    destination_token_address: str | None = None
    source_decimals: int | None = None
    destination_decimals: int | None = None
    source_name: str | None = None
    destination_name: str | None = None
    source_logo_url: str | None = None
    destination_logo_url: str | None = None
    input_amount: str | None = None
    input_amount_raw: str | None = None
    sender_address: str | None = None
    recipient_address: str | None = None
    refund_address: str | None = None
    slippage_bps: int | None = None
    expires_at: str | None = None
    transfer_chain: str | None = None
    transfer_symbol: str | None = None
    transfer_token_address: str | None = None
    transfer_decimals: int | None = None
    transfer_amount: str | None = None
    transfer_amount_raw: str | None = None
    transfer_sender: str | None = None
    transfer_recipient: str | None = None
    transaction_chain: str | None = None
    transaction_hash: str | None = None
    portfolio_chain: str | None = None
    gas_chain: str | None = None
    gas_to: str | None = None
    gas_data: str | None = None
    asset_chain: str | None = None
    asset_search: str | None = None


def build_application(settings: Settings | None = None) -> Any:
    settings = settings or Settings()
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - dependency is locked in production
        raise RuntimeError("langchain-openai is required to run the service") from exc

    api_key = settings.deepseek_api_key or settings.openai_api_key
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY or OPENAI_API_KEY is required")
    model_clients = {
        model_id: ChatOpenAI(
            model=model_id,
            api_key=api_key,
            base_url=settings.openai_base_url,
        ).with_structured_output(IntentOutput, method="json_mode")
        for model_id in dict.fromkeys([settings.openai_model, *settings.allowed_model_ids])
    }
    model_registry = ModelRegistry(model_clients, default_model_id=settings.openai_model)
    model = ModelRouter(model_registry)
    providers: dict[str, Any] = {}
    transports: list[HttpJsonTransport] = []
    price_provider = None
    default_coingecko_base_url = "https://pro-api.coingecko.com/api/v3"
    if settings.bridgers_enabled:
        if not settings.bridgers_base_url:
            raise ValueError("BRIDGERS_BASE_URL is required when BRIDGERS_ENABLED=true")
        transport = HttpJsonTransport(
            settings.bridgers_base_url, timeout_seconds=settings.http_timeout_seconds
        )
        transports.append(transport)
        providers["bridgers"] = BridgersProvider.from_transport(
            transport,
            source_flag=settings.bridgers_source_flag,
            spender_by_chain=settings.bridgers_spender_by_chain,
            swap_spender=settings.bridgers_swap_spender,
        )
    if settings.omnibridge_enabled:
        if not settings.omnibridge_base_url:
            raise ValueError("OMNIBRIDGE_BASE_URL is required when OMNIBRIDGE_ENABLED=true")
        transport = HttpJsonTransport(
            settings.omnibridge_base_url, timeout_seconds=settings.http_timeout_seconds
        )
        transports.append(transport)
        providers["omnibridge"] = OmniBridgeProvider.from_transport(
            transport,
            source_flag=settings.omnibridge_source_flag,
            spender_by_chain=settings.omnibridge_spender_by_chain,
            swap_spender=settings.omnibridge_swap_spender,
        )
    if (
        settings.coingecko_api_key
        or settings.coingecko_token_ids
        or settings.coingecko_native_ids
        or settings.coingecko_base_url != default_coingecko_base_url
    ):
        price_transport = HttpJsonTransport(
            settings.coingecko_base_url, timeout_seconds=settings.http_timeout_seconds
        )
        transports.append(price_transport)
        price_provider = CoinGeckoPriceProvider(
            price_transport,
            token_id_by_address=settings.coingecko_token_ids,
            native_id_by_symbol=settings.coingecko_native_ids,
            ttl_seconds=settings.price_cache_ttl_seconds,
            api_key=settings.coingecko_api_key,
            base_url=settings.coingecko_base_url,
        )
    chain_registry = build_default_registry(
        rpc_urls=settings.rpc_urls,
        rpc_timeout_seconds=settings.rpc_timeout_seconds,
        rpc_max_attempts=settings.rpc_max_attempts,
    )
    checkpoint_handle = initialize_checkpointer(settings.persistence_url)
    graph = build_graph(
        model=model,
        providers=providers,
        chains=dict(chain_registry.items()),
        price_provider=price_provider,
        checkpointer=checkpoint_handle.checkpointer,
        max_poll_attempts=settings.poll_max_attempts,
        confirmation_ttl_seconds=settings.confirmation_ttl_seconds,
    )
    session_store = SqliteSessionStore(settings.persistence_url)
    application = create_app(
        graph=graph,
        providers=providers,
        price_provider=price_provider,
        store=session_store,
        model_registry=model_registry,
        token_verifier=StaticTokenVerifier(settings.auth_tokens) if settings.auth_tokens else None,
        require_auth=settings.auth_required,
        chain_registry=chain_registry,
    )
    application.state.checkpointer_handle = checkpoint_handle
    application.state.transports = transports
    return application


try:  # Keep `uvicorn wallet_agent.main:app` import-safe without .env.
    app = build_application()
except Exception:
    app = create_app()
