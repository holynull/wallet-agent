"""Opt-in, read-only contract checks against the configured external APIs.

These tests deliberately require an explicit ``<PREFIX>_INTEGRATION=1`` flag.
The configuration values are used only to make requests; failures describe
response *shape* and timing, never response values, credentials, addresses, or
request payloads.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import pytest

from tests.helpers.contracts import (
    assert_asset_identity,
    assert_decimal_in_range,
    assert_provider_metadata,
    assert_raw_matches_human,
    safe_shape,
)
from wallet_agent.domain.models import Asset, AssetQuery, SwapQuoteRequest
from wallet_agent.okx import OKX_CHAIN_INDEX_BY_NAME, OkxSignedClient, OkxWalletAdapter
from wallet_agent.providers.bridgers import BridgersProvider
from wallet_agent.providers.http import HttpJsonTransport
from wallet_agent.providers.omnibridge import OmniBridgeProvider


def _live_config(prefix: str, required: tuple[str, ...]) -> dict[str, str] | None:
    """Return opt-in configuration using unprefixed keys.

    ``required`` contains suffixes such as ``("API_KEY", "BASE_URL")`` and
    values are read from ``<PREFIX>_<SUFFIX>``.  Keeping the returned mapping
    suffix-keyed avoids accidentally putting complete environment names (or
    their values) into diagnostics.
    """

    normalized_prefix = prefix.strip().upper()
    if not normalized_prefix or os.getenv(f"{normalized_prefix}_INTEGRATION", "").strip() != "1":
        return None
    values: dict[str, str] = {}
    for suffix in required:
        key = str(suffix).strip().upper()
        env_name = key if key.startswith(f"{normalized_prefix}_") else f"{normalized_prefix}_{key}"
        values[key] = os.getenv(env_name, "").strip()
    return values if all(values.values()) else None


def _optional_config(prefix: str, suffix: str) -> str | None:
    key = f"{prefix.strip().upper()}_{suffix.strip().upper()}"
    value = os.getenv(key, "").strip()
    return value or None


def _timeout_seconds() -> float:
    try:
        configured = float(os.getenv("INTEGRATION_TIMEOUT_SECONDS", "10"))
    except (TypeError, ValueError):
        configured = 10.0
    return min(15.0, max(1.0, configured))


def _safe_code(error: BaseException) -> str:
    value = getattr(error, "code", None) or getattr(error, "status_code", None)
    if value is None:
        value = type(error).__name__
    # Error codes are provider-controlled. Keep only a bounded identifier.
    return re.sub(r"[^A-Za-z0-9_.-]", "?", str(value))[:64]


def _diagnostic(
    endpoint: str,
    started: float,
    *,
    error: BaseException | None = None,
    response: object | None = None,
) -> str:
    code = _safe_code(error) if error is not None else "assertion"
    elapsed_ms = int(max(0.0, (time.perf_counter() - started) * 1000))
    return (
        f"endpoint={endpoint} code={code} shape={safe_shape(response)} "
        f"elapsed_ms={elapsed_ms}"
    )


async def _observe(
    endpoint: str,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    started = time.perf_counter()
    try:
        return await operation()
    except Exception as exc:  # pragma: no cover - exercised with live services
        # Deliberately omit exception text: provider messages can echo payloads.
        pytest.fail(_diagnostic(endpoint, started, error=exc), pytrace=False)


def _required_provider_config(prefix: str) -> dict[str, str] | None:
    return _live_config(
        prefix,
        (
            "BASE_URL",
            "SOURCE_FLAG",
            "INTEGRATION_ADDRESS",
            "SOURCE_CHAIN",
            "SOURCE_SYMBOL",
            "SOURCE_DECIMALS",
            "DESTINATION_CHAIN",
            "DESTINATION_SYMBOL",
            "DESTINATION_DECIMALS",
            "INPUT_AMOUNT",
        ),
    )


def _integer_config(config: Mapping[str, str], key: str, *, field: str) -> int:
    try:
        value = int(config[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise AssertionError(f"{field}: invalid configuration") from exc
    return assert_decimal_in_range(value, field=field)


def _quote_request(prefix: str, config: Mapping[str, str]) -> SwapQuoteRequest:
    source_decimals = _integer_config(config, "SOURCE_DECIMALS", field="source_decimals")
    destination_decimals = _integer_config(
        config, "DESTINATION_DECIMALS", field="destination_decimals"
    )
    try:
        input_amount = Decimal(config["INPUT_AMOUNT"])
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise AssertionError("input_amount: invalid configuration") from exc
    if not input_amount.is_finite() or input_amount <= 0:
        raise AssertionError("input_amount: invalid configuration")
    input_raw_decimal = input_amount * (Decimal(10) ** source_decimals)
    if input_raw_decimal != input_raw_decimal.to_integral_value():
        raise AssertionError("input_amount: precision exceeds source_decimals")
    input_raw = str(int(input_raw_decimal))
    assert_raw_matches_human(input_amount, input_raw, source_decimals, field="input_amount")

    source_address = _optional_config(prefix, "SOURCE_TOKEN_ADDRESS") or _optional_config(
        prefix, "SOURCE_ADDRESS"
    )
    destination_address = _optional_config(
        prefix, "DESTINATION_TOKEN_ADDRESS"
    ) or _optional_config(prefix, "DESTINATION_ADDRESS")
    sender = config["INTEGRATION_ADDRESS"]
    return SwapQuoteRequest(
        source_asset=Asset(
            chain=config["SOURCE_CHAIN"],
            symbol=config["SOURCE_SYMBOL"],
            decimals=source_decimals,
            address=source_address,
        ),
        destination_asset=Asset(
            chain=config["DESTINATION_CHAIN"],
            symbol=config["DESTINATION_SYMBOL"],
            decimals=destination_decimals,
            address=destination_address,
        ),
        input_amount=input_amount,
        input_amount_raw=input_raw,
        sender_address=sender,
        recipient_address=sender,
        refund_address=sender,
    )


async def _provider_smoke(prefix: str, provider_type: type[Any]) -> None:
    config = _required_provider_config(prefix)
    if config is None:
        pytest.skip(
            f"set {prefix}_INTEGRATION=1 and complete {prefix} read-only smoke configuration"
        )
    try:
        request = _quote_request(prefix, config)
    except (AssertionError, KeyError, ValueError, TypeError):
        pytest.fail(
            "endpoint=config code=invalid_configuration shape={} elapsed_ms=0",
            pytrace=False,
        )

    transport = HttpJsonTransport(
        config["BASE_URL"],
        timeout_seconds=_timeout_seconds(),
        max_attempts=1,
        retry_delay_seconds=0,
    )
    provider = provider_type.from_transport(transport, source_flag=config["SOURCE_FLAG"])
    try:
        assets = await _observe(
            f"{prefix.lower()}:catalog",
            lambda: provider.list_assets(
                AssetQuery(chain=request.source_asset.chain, search=request.source_asset.symbol)
            ),
        )
        if not isinstance(assets, list):
            pytest.fail(
                _diagnostic(f"{prefix.lower()}:catalog", time.perf_counter()),
                pytrace=False,
            )
        for asset in assets:
            assert_decimal_in_range(asset.decimals, field="catalog_decimals")

        quote = await _observe(f"{prefix.lower()}:quote", lambda: provider.quote(request))
        assert_provider_metadata(
            quote.model_dump(mode="python"),
            provider=prefix.lower(),
            required_keys=("provider_reference",),
        )
        assert_asset_identity(
            quote.source_asset.model_dump(mode="python"),
            chain=request.source_asset.chain,
            symbol=request.source_asset.symbol,
            address=request.source_asset.address,
        )
        assert_asset_identity(
            quote.destination_asset.model_dump(mode="python"),
            chain=request.destination_asset.chain,
            symbol=request.destination_asset.symbol,
            address=request.destination_asset.address,
        )
        assert_decimal_in_range(quote.destination_asset.decimals, field="quote_decimals")
        assert_raw_matches_human(
            quote.expected_output,
            quote.expected_output_raw,
            quote.destination_asset.decimals,
            field="expected_output",
        )
    finally:
        await transport.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_okx_read_only_adapter_contract_smoke():
    config = _live_config(
        "OKX",
        (
            "API_KEY",
            "SECRET_KEY",
            "PASSPHRASE",
            "PROJECT_ID",
            "INTEGRATION_ADDRESS",
        ),
    )
    if config is None:
        pytest.skip("set OKX_INTEGRATION=1 and complete OKX read-only smoke configuration")
    client = OkxSignedClient(
        os.getenv("OKX_BASE_URL", "https://web3.okx.com"),
        api_key=config["API_KEY"],
        secret_key=config["SECRET_KEY"],
        passphrase=config["PASSPHRASE"],
        project_id=config["PROJECT_ID"],
        timeout_seconds=10,
        max_attempts=1,
    )
    adapter = OkxWalletAdapter(client, OKX_CHAIN_INDEX_BY_NAME)
    try:
        total = await _observe(
            "okx:total-value",
            lambda: adapter.get_total_value(config["INTEGRATION_ADDRESS"], ["ETH"]),
        )
        assert total.provider == "okx"
        assert total.total_value.is_finite() and total.total_value >= 0
        balances = await _observe(
            "okx:token-balances",
            lambda: adapter.get_token_balances(config["INTEGRATION_ADDRESS"], ["ETH"]),
        )
        if not isinstance(balances, list):
            pytest.fail(
                "endpoint=okx:token-balances code=assertion shape={} elapsed_ms=0",
                pytrace=False,
            )
        for balance in balances:
            assert balance.provider == "okx"
            assert_decimal_in_range(balance.asset.decimals, field="balance_decimals")
            assert_raw_matches_human(
                balance.amount,
                balance.amount_raw,
                balance.asset.decimals,
                field="balance_amount",
            )
    finally:
        await client.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_bridgers_catalog_and_quote_contract_smoke():
    await _provider_smoke("BRIDGERS", BridgersProvider)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_omnibridge_catalog_and_quote_contract_smoke():
    await _provider_smoke("OMNIBRIDGE", OmniBridgeProvider)
