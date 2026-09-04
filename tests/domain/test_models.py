from decimal import Decimal

from wallet_agent.domain.models import (
    Asset,
    DepositOrder,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    TokenBalance,
    UnsignedTransaction,
    WalletSnapshot,
)


def test_unsigned_transaction_contains_no_signer_material():
    transaction = UnsignedTransaction(
        chain="EVM",
        chain_id=1,
        to="0x0000000000000000000000000000000000000001",
        data="0x",
        value="0x0",
        display={"input_amount": "1", "output_amount": "2"},
    )

    assert "private_key" not in transaction.model_dump()


def test_quote_models_preserve_human_and_raw_amounts_as_serializable_values():
    usdc = Asset(chain="EVM", chain_id=1, symbol="USDC", decimals=6)
    request = SwapQuoteRequest(
        source_asset=usdc,
        destination_asset=Asset(chain="EVM", chain_id=1, symbol="WETH", decimals=18),
        input_amount=Decimal("1.25"),
        input_amount_raw="1250000",
        sender_address="0x0000000000000000000000000000000000000001",
        recipient_address="0x0000000000000000000000000000000000000002",
    )
    quote = NormalizedQuote(
        provider="bridgers",
        source_asset=usdc,
        destination_asset=request.destination_asset,
        input_amount=Decimal("1.25"),
        input_amount_raw="1250000",
        expected_output=Decimal("0.0005"),
        expected_output_raw="500000000000000",
        minimum_output_raw="490000000000000",
        provider_reference="quote-123",
    )

    assert request.model_dump(mode="json")["input_amount"] == "1.25"
    assert quote.model_dump(mode="json")["minimum_output_raw"] == "490000000000000"


def test_order_models_keep_provider_payloads_redacted_and_expiry_explicit():
    deposit = DepositOrder(
        provider="omnibridge",
        provider_order_id="order-123",
        deposit_address="0x0000000000000000000000000000000000000003",
        source_asset=Asset(chain="EVM", chain_id=1, symbol="USDC", decimals=6),
        input_amount=Decimal("10"),
        input_amount_raw="10000000",
        expires_at=None,
        provider_reference="order-123",
        provider_payload={"request_id": "request-123"},
    )
    provider_order = ProviderOrder(
        provider="omnibridge",
        provider_order_id="order-123",
        provider_reference="order-123",
    )
    status = NormalizedOrderStatus(
        provider="omnibridge",
        provider_order_id="order-123",
        status="processing",
        provider_reference="order-123",
    )

    assert deposit.expires_at is None
    assert deposit.provider_payload == {"request_id": "request-123"}
    assert provider_order.tx_hash is None
    assert status.completed_at is None


def test_wallet_snapshot_uses_decimal_human_balance_and_raw_integer_balance():
    snapshot = WalletSnapshot(
        address="0x0000000000000000000000000000000000000001",
        chain="EVM",
        chain_id=1,
        native_balance=TokenBalance(
            asset=Asset(chain="EVM", chain_id=1, symbol="ETH", decimals=18),
            amount=Decimal("1.5"),
            amount_raw="1500000000000000000",
        ),
    )

    assert snapshot.model_dump(mode="json")["native_balance"]["amount"] == "1.5"
