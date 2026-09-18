"""Provider and chain protocols consumed by the orchestration layer."""

from typing import Protocol

from .chains import CapabilitySnapshot
from .models import (
    Asset,
    AssetQuery,
    DepositOrder,
    FeeEstimate,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    TokenBalance,
    TokenPrice,
    TransactionRecord,
    TransactionStatus,
    UnsignedTransaction,
)


class SwapProvider(Protocol):
    """Normalized interface for a swap provider's supported lifecycle."""

    async def list_assets(self, query: AssetQuery) -> list[Asset]: ...

    async def quote(self, request: SwapQuoteRequest) -> NormalizedQuote: ...

    async def prepare(self, quote: NormalizedQuote) -> UnsignedTransaction | DepositOrder: ...

    async def register_broadcast(self, provider_reference: str, tx_hash: str) -> ProviderOrder: ...

    async def get_status(self, order: ProviderOrder) -> NormalizedOrderStatus: ...


class ChainAdapter(Protocol):
    """Read-only, explicitly capability-scoped chain access."""

    @property
    def capabilities(self) -> CapabilitySnapshot: ...

    async def validate_address(self, address: str) -> bool: ...

    async def get_token_balance(self, asset: Asset, owner: str) -> TokenBalance: ...

    async def get_native_balance(self, address: str) -> TokenBalance: ...

    async def get_token_balances(self, address: str) -> list[TokenBalance]: ...

    async def get_allowance(self, token: Asset, owner: str, spender: str) -> str: ...

    async def get_transaction_receipt(self, tx_hash: str) -> dict[str, object] | None: ...

    async def get_transaction(self, tx_hash: str) -> dict[str, object] | None: ...

    async def get_transaction_history(
        self, address: str, *, limit: int = 20
    ) -> list[TransactionRecord]: ...

    async def estimate_fee(
        self, *, to: str | None = None, data: str | None = None
    ) -> FeeEstimate: ...

    async def get_transaction_status(self, tx_hash: str) -> TransactionStatus: ...

    def build_native_transfer(
        self, *, from_address: str, to_address: str, amount_raw: str
    ) -> UnsignedTransaction: ...

    def build_erc20_transfer(
        self, *, token: Asset, from_address: str, to_address: str, amount_raw: str
    ) -> UnsignedTransaction: ...

    def build_erc20_approve(
        self, *, token: Asset, owner: str, spender: str, amount_raw: str
    ) -> UnsignedTransaction: ...


class TokenPriceProvider(Protocol):
    """Normalized interface for batch asset price lookup."""

    async def get_prices(self, assets: list[Asset]) -> list[TokenPrice]: ...
