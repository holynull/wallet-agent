"""Serializable models shared by wallet, provider, and orchestration layers."""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

ProviderName = Literal["bridgers", "omnibridge"]
OrderState = Literal[
    "pending",
    "processing",
    "broadcasted",
    "completed",
    "failed",
    "refunded",
    "kyc_required",
    "timed_out",
    "cancelled",
]
RawInteger = Annotated[str, Field(pattern=r"^[0-9]+$")]

_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "authorization",
        "authentication",
        "api_key",
        "apikey",
        "api_token",
        "apitoken",
        "access_token",
        "accesstoken",
        "auth_token",
        "authtoken",
        "bearer_token",
        "bearertoken",
        "client_secret",
        "clientsecret",
        "credential",
        "credentials",
        "id_token",
        "idtoken",
        "mnemonic",
        "password",
        "private_key",
        "privatekey",
        "refresh_token",
        "refreshtoken",
        "secret",
        "seed",
        "seed_phrase",
        "seedphrase",
        "session_token",
        "sessiontoken",
        "signer",
        "token",
        "secret_key",
        "secretkey",
        "api_secret",
        "apisecret",
        "authorization_header",
        "authorizationheader",
        "mnemonic_words",
        "mnemonicwords",
        "wallet_client",
        "walletclient",
    }
)

_SENSITIVE_CANONICAL_KEYS = frozenset(
    "".join(character for character in key.lower() if character.isalnum())
    for key in _SENSITIVE_METADATA_KEYS
)


class DomainModel(BaseModel):
    """Base model that rejects extra fields and redacts arbitrary metadata."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def redact_sensitive_mappings(cls, value: Any) -> Any:
        return _redact_metadata(value)


def _redact_metadata(value: Any, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_metadata(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_metadata(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    canonical_key = "".join(character for character in key.lower() if character.isalnum())
    return canonical_key in _SENSITIVE_CANONICAL_KEYS or any(
        fragment in canonical_key
        for fragment in ("privatekey", "seedphrase", "clientsecret", "accesstoken", "refreshtoken")
    )


class Asset(DomainModel):
    chain: str
    chain_id: int | str | None = None
    symbol: str
    decimals: int = Field(ge=0, le=255)
    address: str | None = None
    name: str | None = None
    logo_url: str | None = None


class AssetQuery(DomainModel):
    chain: str | None = None
    search: str | None = None
    provider: ProviderName | None = None


class SwapQuoteRequest(DomainModel):
    source_asset: Asset
    destination_asset: Asset
    input_amount: Decimal = Field(gt=0)
    input_amount_raw: RawInteger
    sender_address: str
    recipient_address: str
    refund_address: str | None = None
    slippage_bps: int = Field(default=50, ge=0, le=10_000)
    expires_at: datetime | None = None


class NormalizedQuote(DomainModel):
    provider: ProviderName
    source_asset: Asset
    destination_asset: Asset
    input_amount: Decimal = Field(gt=0)
    input_amount_raw: RawInteger
    expected_output: Decimal = Field(ge=0)
    expected_output_raw: RawInteger
    minimum_output: Decimal | None = Field(default=None, ge=0)
    minimum_output_raw: RawInteger | None = None
    provider_fee: Decimal | None = Field(default=None, ge=0)
    provider_fee_raw: RawInteger | None = None
    network_fee: Decimal | None = Field(default=None, ge=0)
    network_fee_raw: RawInteger | None = None
    expires_at: datetime | None = None
    provider_reference: str
    provider_payload: dict[str, JsonValue] = Field(default_factory=dict)


class UnsignedTransaction(DomainModel):
    chain: str
    chain_id: int | str | None = None
    to: str
    data: str
    value: str
    gas_limit: RawInteger | None = None
    max_fee_per_gas: RawInteger | None = None
    max_priority_fee_per_gas: RawInteger | None = None
    expires_at: datetime | None = None
    provider: ProviderName | None = None
    provider_reference: str | None = None
    display: dict[str, str] = Field(default_factory=dict)


class DepositOrder(DomainModel):
    provider: ProviderName
    provider_order_id: str
    deposit_address: str
    source_asset: Asset
    destination_asset: Asset | None = None
    input_amount: Decimal = Field(gt=0)
    input_amount_raw: RawInteger
    recipient_address: str | None = None
    refund_address: str | None = None
    expires_at: datetime | None = None
    provider_reference: str
    provider_payload: dict[str, JsonValue] = Field(default_factory=dict)


class ProviderOrder(DomainModel):
    provider: ProviderName
    provider_order_id: str
    provider_reference: str
    tx_hash: str | None = None
    expires_at: datetime | None = None
    provider_payload: dict[str, JsonValue] = Field(default_factory=dict)


class NormalizedOrderStatus(DomainModel):
    provider: ProviderName
    provider_order_id: str
    status: OrderState
    provider_reference: str
    message: str | None = None
    tx_hash: str | None = None
    refund_address: str | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    provider_payload: dict[str, JsonValue] = Field(default_factory=dict)


class TokenBalance(DomainModel):
    asset: Asset
    amount: Decimal = Field(ge=0)
    amount_raw: RawInteger
    usd_value: Decimal | None = Field(default=None, ge=0)


class TokenPrice(DomainModel):
    asset: Asset
    usd_price: Decimal = Field(ge=0)
    observed_at: datetime | None = None


class WalletSnapshot(DomainModel):
    address: str
    chain: str
    chain_id: int | str | None = None
    native_balance: TokenBalance | None = None
    token_balances: list[TokenBalance] = Field(default_factory=list)
    observed_at: datetime | None = None


class TransactionStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    DROPPED = "dropped"
    UNKNOWN = "unknown"


class TransactionRecord(DomainModel):
    chain: str
    chain_id: int | str | None = None
    tx_hash: str
    status: TransactionStatus
    from_address: str | None = None
    to_address: str | None = None
    value: str | None = None
    block_number: int | None = Field(default=None, ge=0)
    confirmed_at: datetime | None = None


class FeeEstimate(DomainModel):
    chain: str
    chain_id: int | str | None = None
    asset: Asset
    amount: Decimal = Field(ge=0)
    amount_raw: RawInteger
    gas_limit: RawInteger | None = None
    expires_at: datetime | None = None


class AgentError(DomainModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)
