from decimal import Decimal

import pytest

from wallet_agent.domain.models import (
    AgentError,
    AllowanceRequirement,
    ApprovalTransaction,
    Asset,
    DepositOrder,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    SwapAuthorizationState,
    SwapQuoteRequest,
    TokenBalance,
    TokenPrice,
    TransferRequest,
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
        provider_payload={
            "request_id": "request-123",
            "privateKey": "must-not-be-stored",
            "nested": {"client_secret": "must-not-be-stored"},
        },
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
    assert deposit.provider_payload == {
        "request_id": "request-123",
        "privateKey": "[REDACTED]",
        "nested": {"client_secret": "[REDACTED]"},
    }
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


def test_arbitrary_domain_mappings_redact_canonicalized_secret_key_variants():
    transaction = UnsignedTransaction(
        chain="EVM",
        chain_id=1,
        to="0x0000000000000000000000000000000000000001",
        data="0x",
        value="0x0",
        display={"seedPhrase": "must-not-be-stored", "input_amount": "1"},
    )
    error = AgentError(
        code="PROVIDER_FAILURE",
        message="Provider request failed.",
        details={
            "access_token": "must-not-be-stored",
            "apiToken": "must-not-be-stored",
            "nested": {"clientSecret": "must-not-be-stored"},
        },
    )

    assert transaction.display == {"seedPhrase": "[REDACTED]", "input_amount": "1"}
    assert error.details == {
        "access_token": "[REDACTED]",
        "apiToken": "[REDACTED]",
        "nested": {"clientSecret": "[REDACTED]"},
    }


def test_arbitrary_domain_mappings_redact_common_sensitive_key_variants():
    error = AgentError(
        code="PROVIDER_FAILURE",
        message="Provider request failed.",
        details={
            "token": "must-not-be-stored",
            "TOKEN": "must-not-be-stored",
            "secretKey": "must-not-be-stored",
            "secret_key": "must-not-be-stored",
            "secret-key": "must-not-be-stored",
            "apiSecret": "must-not-be-stored",
            "api_secret": "must-not-be-stored",
            "api-secret": "must-not-be-stored",
            "authorizationHeader": "must-not-be-stored",
            "authorization_header": "must-not-be-stored",
            "authorization-header": "must-not-be-stored",
            "mnemonicWords": "must-not-be-stored",
            "mnemonic_words": "must-not-be-stored",
            "mnemonic-words": "must-not-be-stored",
            "nested": {"ApiSecret": "must-not-be-stored"},
            "token_symbol": "USDC",
            "secretariat": "support-team",
        },
    )

    assert error.details == {
        "token": "[REDACTED]",
        "TOKEN": "[REDACTED]",
        "secretKey": "[REDACTED]",
        "secret_key": "[REDACTED]",
        "secret-key": "[REDACTED]",
        "apiSecret": "[REDACTED]",
        "api_secret": "[REDACTED]",
        "api-secret": "[REDACTED]",
        "authorizationHeader": "[REDACTED]",
        "authorization_header": "[REDACTED]",
        "authorization-header": "[REDACTED]",
        "mnemonicWords": "[REDACTED]",
        "mnemonic_words": "[REDACTED]",
        "mnemonic-words": "[REDACTED]",
        "nested": {"ApiSecret": "[REDACTED]"},
        "token_symbol": "USDC",
        "secretariat": "support-team",
    }


def test_transfer_request_normalizes_amount_and_requires_evm_addresses():
    request = TransferRequest(
        chain="BASE",
        sender="0x" + "1" * 40,
        recipient="0x" + "2" * 40,
        amount="1.25",
        amount_raw="1250000000000000000",
        token=None,
    )

    assert request.amount_raw == "1250000000000000000"
    assert request.amount == Decimal("1.25")


def test_transfer_request_rejects_non_evm_sender_address():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TransferRequest(
            chain="BASE",
            sender="not-an-evm-address",
            recipient="0x" + "2" * 40,
            amount="1.25",
            amount_raw="1250000000000000000",
            token=None,
        )


def test_allowance_requirement_is_serializable_in_normalized_quote():
    requirement = AllowanceRequirement(
        token=Asset(chain="BASE", symbol="USDC", decimals=6, address="0x" + "3" * 40),
        owner="0x" + "1" * 40,
        spender="0x" + "4" * 40,
        required_amount_raw="1000000",
        current_allowance_raw="0",
    )
    quote = NormalizedQuote(
        provider="bridgers",
        source_asset=Asset(
            chain="BASE", symbol="USDC", decimals=6, address="0x" + "3" * 40
        ),
        destination_asset=Asset(
            chain="BSC", symbol="USDT", decimals=6, address="0x" + "5" * 40
        ),
        input_amount="1",
        input_amount_raw="1000000",
        expected_output="0.99",
        expected_output_raw="990000",
        provider_reference="quote-1",
        allowance_requirement=requirement,
    )

    assert quote.model_dump(mode="json")["allowance_requirement"]["spender"] == "0x" + "4" * 40


def test_price_and_authorization_contracts_serialize_nested_wallet_actions():
    token = Asset(chain="BASE", symbol="USDC", decimals=6, address="0x" + "3" * 40)
    requirement = AllowanceRequirement(
        token=token,
        owner="0x" + "1" * 40,
        spender="0x" + "4" * 40,
        required_amount_raw="1000000",
        current_allowance_raw="0",
    )
    approval = ApprovalTransaction(
        chain="BASE",
        to="0x" + "3" * 40,
        data="0x095ea7b3",
        value="0",
        token=token,
        owner="0x" + "1" * 40,
        spender="0x" + "4" * 40,
        amount_raw="1000000",
    )
    state = SwapAuthorizationState(
        stage="approval_required",
        allowance_requirement=requirement,
        approval_transaction=approval,
    )
    price = TokenPrice(asset=token, usd_price="1.00")

    assert price.model_dump(mode="json")["usd_price"] == "1.00"
    assert state.model_dump(mode="json")["approval_transaction"]["data"] == "0x095ea7b3"
