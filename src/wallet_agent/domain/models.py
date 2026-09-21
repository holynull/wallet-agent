"""Serializable models shared by wallet, provider, and orchestration layers."""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

ProviderName = Literal["bridgers", "omnibridge", "okx", "coingecko"]
OrderState = Literal[
    "pending",
    "processing",
    "broadcasted",
    "broadcast_seen",
    "broadcast_pending",
    "not_propagated",
    "confirmed",
    "completed",
    "failed",
    "dropped_or_replaced",
    "unknown",
    "refunded",
    "kyc_required",
    "timed_out",
    "cancelled",
]
RawInteger = Annotated[str, Field(pattern=r"^[0-9]+$")]
EVMAddress = Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$")]

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
        if not isinstance(value, Mapping):
            return _redact_metadata(value)
        protected_keys = frozenset(cls.model_fields)
        return {
            str(item_key): (
                item_value
                if _is_nested_model_field(cls.model_fields.get(str(item_key)))
                else _redact_metadata(item_value, str(item_key), protected_keys)
            )
            for item_key, item_value in value.items()
        }


def _redact_metadata(
    value: Any, key: str | None = None, protected_keys: frozenset[str] | None = None
) -> Any:
    if (
        key is not None
        and _is_sensitive_key(key)
        and (protected_keys is None or key not in protected_keys)
    ):
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


def _is_nested_model_field(field: Any) -> bool:
    """Keep mappings for typed nested models intact for their own validation.

    Without this boundary, a legitimate ``Asset.token`` field nested inside a
    quote is mistaken for credential metadata and replaced with ``[REDACTED]``.
    The nested DomainModel then applies redaction to its arbitrary fields.
    """
    if field is None:
        return False
    annotation = getattr(field, "annotation", field)
    candidates = get_args(annotation) or (annotation,)
    for candidate in candidates:
        if candidate is type(None):
            continue
        origin = get_origin(candidate)
        if origin in (Union,):
            if _is_nested_model_field(candidate):
                return True
        try:
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                return True
        except TypeError:
            continue
    return False


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
    slippage_bps: int = Field(default=100, ge=0, le=10_000)
    expires_at: datetime | None = None


class TransferRequest(DomainModel):
    """A user-authorized native or ERC-20 transfer request."""

    chain: str
    sender: EVMAddress
    recipient: EVMAddress
    amount: Decimal = Field(gt=0)
    amount_raw: RawInteger
    token: Asset | None = None


class AllowanceRequirement(DomainModel):
    """On-chain allowance needed before a provider swap can be prepared."""

    token: Asset
    owner: EVMAddress
    spender: EVMAddress
    required_amount_raw: RawInteger
    current_allowance_raw: RawInteger


class ApprovalTransaction(DomainModel):
    """Unsigned ERC-20 approval transaction and its authorization context."""

    chain: str
    chain_id: int | str | None = None
    to: EVMAddress
    data: str
    value: RawInteger = "0"
    token: Asset
    owner: EVMAddress
    spender: EVMAddress
    amount_raw: RawInteger
    expires_at: datetime | None = None


class TokenPrice(DomainModel):
    """A point-in-time USD price snapshot for an asset."""

    asset: Asset
    usd_price: Decimal = Field(ge=0)
    observed_at: datetime | None = None


AuthorizationStage = Literal[
    "approval_required",
    "swap_ready",
    "swap_prepared",
    "pending",
    "completed",
    "failed",
]


class SwapAuthorizationState(DomainModel):
    """Checkpoint-safe state for the allowance and approval gate."""

    stage: AuthorizationStage
    allowance_requirement: AllowanceRequirement | None = None
    approval_transaction: ApprovalTransaction | None = None
    approval_tx_hash: str | None = None
    pending_transaction: "UnsignedTransaction | None" = None


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
    usd_input_value: Decimal | None = Field(default=None, ge=0)
    usd_expected_output: Decimal | None = Field(default=None, ge=0)
    # Providers may return either an ordered list or an identity-keyed mapping;
    # the graph normalizes both forms for the app response.
    price_snapshots: list[TokenPrice] | dict[str, Any] = Field(default_factory=list)
    allowance_requirement: AllowanceRequirement | None = None
    slippage_bps: int = Field(default=100, ge=0, le=10_000)


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
    is_risk_token: bool = False
    provider: ProviderName | None = None
    observed_at: datetime | None = None
    market_details: "TokenMarketDetails | None" = None


class TokenMarketDetails(DomainModel):
    """Normalized optional market metadata associated with a token balance."""

    asset: Asset | None = None
    usd_price: Decimal | None = Field(default=None, ge=0)
    market_cap: Decimal | None = Field(default=None, ge=0)
    volume_24h: Decimal | None = Field(default=None, ge=0)
    price_change_24h: Decimal | None = None
    liquidity: Decimal | None = Field(default=None, ge=0)
    holder_count: int | None = Field(default=None, ge=0)


class WalletTotalValue(DomainModel):
    """A normalized multi-chain wallet valuation returned by a wallet provider."""

    address: str
    chain_indexes: list[str] = Field(default_factory=list)
    asset_type: str = "0"
    exclude_risk_tokens: bool = True
    total_value: Decimal = Field(ge=0)
    provider: Literal["okx"] = "okx"
    observed_at: datetime | None = None


class TransactionContext(DomainModel):
    """Unsigned transaction context accepted by OKX pre-transaction endpoints."""

    chain: str
    from_address: str
    to_address: str
    native_amount: RawInteger = "0"
    calldata: str = "0x"


_PositiveRawInteger = Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]


class GasLimitEstimate(DomainModel):
    """A validated gas-limit observation from OKX."""

    gas_limit: _PositiveRawInteger
    chain: str | None = None
    provider: Literal["okx"] = "okx"
    observed_at: datetime | None = None


class SimulationResult(DomainModel):
    """Sanitized transaction simulation evidence from OKX."""

    success: bool
    gas_used: RawInteger | None = None
    failure_reason: str | None = None
    chain: str | None = None
    provider: Literal["okx"] = "okx"
    observed_at: datetime | None = None


class TokenPrice(DomainModel):
    asset: Asset
    usd_price: Decimal = Field(ge=0)
    observed_at: datetime | None = None
    provider: ProviderName | None = None


class HistoricalPricePoint(DomainModel):
    observed_at: datetime
    price: Decimal = Field(ge=0)


class HistoricalPricePage(DomainModel):
    points: list[HistoricalPricePoint] = Field(default_factory=list)
    next_cursor: str | None = None


class PriceCandle(DomainModel):
    observed_at: datetime
    open: Decimal = Field(ge=0)
    high: Decimal = Field(ge=0)
    low: Decimal = Field(ge=0)
    close: Decimal = Field(ge=0)
    volume: Decimal = Field(ge=0)
    volume_usd: Decimal = Field(ge=0)
    confirmed: bool


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
    max_fee_per_gas: RawInteger | None = None
    max_priority_fee_per_gas: RawInteger | None = None
    expires_at: datetime | None = None


class AgentError(DomainModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)
