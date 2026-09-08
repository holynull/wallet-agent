"""Provider and chain protocols consumed by the orchestration layer."""

from typing import Protocol

from .chains import CapabilitySnapshot
from .models import (
    Asset,
    AssetQuery,
    TokenPrice,
    DepositOrder,
    FeeEstimate,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    TokenBalance,
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

    async def get_native_balance(self, address: str) -> TokenBalance: ...

    async def get_token_balances(self, address: str) -> list[TokenBalance]: ...

    async def get_transaction_history(
        self, address: str, *, limit: int = 20
    ) -> list[TransactionRecord]: ...

    async def estimate_fee(
        self, *, to: str | None = None, data: str | None = None
    ) -> FeeEstimate: ...

    async def get_transaction_status(self, tx_hash: str) -> TransactionStatus: ...


class TokenPriceProvider(Protocol):
    """Normalized interface for batch asset price lookup."""

    def get_prices(self, assets: list[Asset]) -> list[TokenPrice]: ...
