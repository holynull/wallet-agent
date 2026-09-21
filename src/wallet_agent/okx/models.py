"""Normalized models exposed by the OKX wallet adapter.

The canonical definitions live in ``wallet_agent.domain.models`` so graph and
API code can depend on provider-neutral contracts.  These aliases keep the
OKX integration convenient to import without exposing raw OKX response shapes.
"""

from wallet_agent.domain.models import (
    GasLimitEstimate,
    SimulationResult,
    TokenBalance,
    TokenMarketDetails,
    TransactionContext,
    WalletTotalValue,
)

__all__ = [
    "GasLimitEstimate",
    "SimulationResult",
    "TokenBalance",
    "TokenMarketDetails",
    "TransactionContext",
    "WalletTotalValue",
]
