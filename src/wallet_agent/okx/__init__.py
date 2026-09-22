"""Optional OKX Onchain OS integrations."""

from .client import OkxSignedClient
from .errors import OkxClientError
from .explorer import OkxExplorerAdapter, OkxExplorerError
from .wallet import OkxWalletAdapter, OkxWalletError

OKX_CHAIN_INDEX_BY_NAME = {
    "ETH": "1",
    "BSC": "56",
    "POLYGON": "137",
    "MATIC": "137",
    "ARBITRUM": "42161",
    "ARB": "42161",
    "OPTIMISM": "10",
    "OP": "10",
    "BASE": "8453",
    "TRON": "195",
    "SOLANA": "501",
}

__all__ = [
    "OKX_CHAIN_INDEX_BY_NAME",
    "OkxClientError",
    "OkxExplorerAdapter",
    "OkxExplorerError",
    "OkxSignedClient",
    "OkxWalletAdapter",
    "OkxWalletError",
]
