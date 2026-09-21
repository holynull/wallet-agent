from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from wallet_agent.domain.models import (
    Asset,
    GasLimitEstimate,
    SimulationResult,
    TokenBalance,
    TokenMarketDetails,
    TransactionContext,
    WalletTotalValue,
)


def test_wallet_models_normalize_decimal_values_and_timestamps():
    observed_at = datetime(2026, 9, 21, 1, 2, 3, tzinfo=timezone.utc)

    total = WalletTotalValue(
        address="0x" + "1" * 40,
        chain_indexes=["1", "56"],
        total_value="123.4500",
        observed_at=observed_at,
    )
    token = TokenBalance(
        asset=Asset(
            chain="ETH",
            chain_id=1,
            symbol="USDC",
            decimals=6,
            address="0x" + "2" * 40,
        ),
        amount="12.5",
        amount_raw="12500000",
        usd_value="12.50",
        is_risk_token=True,
        provider="okx",
        observed_at=observed_at,
    )

    assert total.total_value == Decimal("123.4500")
    assert total.observed_at == observed_at
    assert token.amount == Decimal("12.5")
    assert token.usd_value == Decimal("12.50")
    assert token.is_risk_token is True
    assert token.provider == "okx"


def test_transaction_context_builds_documented_native_pretransaction_shape():
    transaction = TransactionContext(
        chain="ETH",
        from_address="0x" + "1" * 40,
        to_address="0x" + "2" * 40,
        native_amount="1000000000000000",
        calldata="0xabcdef",
    )

    assert transaction.native_amount == "1000000000000000"
    assert transaction.calldata == "0xabcdef"


def test_gas_limit_rejects_zero_or_non_decimal_values():
    with pytest.raises(ValidationError):
        GasLimitEstimate(gas_limit="0")
    with pytest.raises(ValidationError):
        GasLimitEstimate(gas_limit="0x5208")


def test_simulation_result_keeps_sanitized_failure_evidence():
    result = SimulationResult(
        success=False,
        gas_used="21000",
        failure_reason="execution reverted",
        observed_at="2026-09-21T01:02:03Z",
    )

    assert result.gas_used == "21000"
    assert result.failure_reason == "execution reverted"
    assert result.observed_at == datetime(2026, 9, 21, 1, 2, 3, tzinfo=timezone.utc)


def test_token_market_details_accepts_decimal_metrics():
    details = TokenMarketDetails(
        asset=Asset(chain="ETH", symbol="ETH", decimals=18),
        usd_price="2500.125",
        market_cap="1000000000.5",
        volume_24h="12345.67",
    )

    assert details.usd_price == Decimal("2500.125")
    assert details.market_cap == Decimal("1000000000.5")
    assert details.volume_24h == Decimal("12345.67")
