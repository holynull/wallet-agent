"""Typed chain capability declarations and lookup registry."""

from collections.abc import Iterable
from enum import Enum

from pydantic import Field

from .errors import ChainCapabilityUnavailable
from .models import DomainModel
from .normalization import canonical_chain, canonical_symbol

_NATIVE_DECIMALS = {
    "BTC": 8,
    "TRON": 6,
    "TRX": 6,
    "SOLANA": 9,
    "SOL": 9,
}

_CHAIN_LOGO_BASE = "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains"

_CHAIN_LOGO_SLUGS = {
    "ETH": "ethereum",
    "BASE": "base",
    "ARBITRUM": "arbitrum",
    "ARB": "arbitrum",
    "OPTIMISM": "optimism",
    "OP": "optimism",
    "BSC": "binance",
    "POLYGON": "polygon",
    "AVAXC": "avalanchec",
    "TRON": "tron",
    "TRX": "tron",
    "BTC": "bitcoin",
    "BITCOIN": "bitcoin",
    "SOLANA": "solana",
    "SOL": "solana",
    "CELO": "celo",
    # Trust Wallet spells Avalanche's C-Chain ``avalanchec``; ``avalanche`` 404s.
    "AVALANCHE": "avalanchec",
    "AVAX": "avalanchec",
    "MEGAETH": "megaeth",
}

_NATIVE_SYMBOLS = {
    "BTC": "BTC",
    "BITCOIN": "BTC",
    "EVM": "ETH",
    "ETH": "ETH",
    "BASE": "ETH",
    "ARBITRUM": "ETH",
    "ARB": "ETH",
    "OPTIMISM": "ETH",
    "OP": "ETH",
    "BSC": "BNB",
    "POLYGON": "MATIC",
    "TRON": "TRX",
    "TRX": "TRX",
    "SOLANA": "SOL",
    "SOL": "SOL",
    "AVALANCHE": "AVAX",
    "AVAX": "AVAX",
}


def chain_logo_url(chain: str) -> str | None:
    """A stable CDN logo URL for a supported chain, ``None`` when unknown."""
    slug = _CHAIN_LOGO_SLUGS.get(canonical_chain(chain))
    return f"{_CHAIN_LOGO_BASE}/{slug}/info/logo.png" if slug else None


def native_symbol(chain: str) -> str | None:
    """The chain's native coin symbol, ``None`` when unknown."""
    return _NATIVE_SYMBOLS.get(canonical_chain(chain))


def token_logo_url(
    chain: str, symbol: str, *, fallback: str | None = None
) -> str | None:
    """A token's logo: the provider logo wins, else a native coin reuses its
    chain logo; other tokens stay empty for the App to render a placeholder."""
    if fallback:
        return fallback
    native = native_symbol(chain)
    if native and symbol and canonical_symbol(str(symbol)) == canonical_symbol(native):
        return chain_logo_url(chain)
    return None


def asset_logo_url(chain: str, contract_address: str | None) -> str | None:
    """Trust Wallet logo URL for a contract-backed token (ERC20/TRC20 etc).

    Returns ``None`` when the chain slug or contract address is unknown, so the
    App renders its own placeholder instead of a broken image.
    """
    if not contract_address:
        return None
    slug = _CHAIN_LOGO_SLUGS.get(canonical_chain(chain))
    if not slug:
        return None
    return f"{_CHAIN_LOGO_BASE}/{slug}/assets/{contract_address}/logo.png"


def asset_logo_fields(
    chain: str | None, symbol: str | None = None, logo_url: str | None = None
) -> dict[str, str | None]:
    """The two logo fields an App asset row renders.

    ``logo_url`` is the asset icon: the provider logo when one exists, an
    equivalent native coin's chain logo, otherwise ``None`` so the App draws its
    placeholder. ``chain_logo_url`` is the chain badge next to it and is filled
    for every known chain. Callers spread this into whichever row, suggestion, or
    card carries a ``chain``/``symbol`` pair so App data keeps one convention.
    """
    chain_text = str(chain or "")
    if not chain_text:
        return {"logo_url": None, "chain_logo_url": None}
    return {
        "logo_url": token_logo_url(
            chain_text, str(symbol or ""), fallback=(str(logo_url) if logo_url else None)
        ),
        "chain_logo_url": chain_logo_url(chain_text),
    }


def native_decimals(chain: str) -> int:
    """Decimals of a chain's native asset, defaulting to the EVM 18."""
    normalized = canonical_chain(chain)
    return _NATIVE_DECIMALS.get(normalized, 18)


class Capability(str, Enum):
    ADDRESS_VALIDATION = "address_validation"
    BALANCES = "balances"
    HISTORY = "history"
    FEES = "fees"
    TRANSACTION_STATUS = "transaction_status"


class CapabilitySnapshot(DomainModel):
    chain: str
    available: frozenset[Capability] = Field(default_factory=frozenset)

    def supports(self, capability: Capability) -> bool:
        return capability in self.available


class CapabilityRegistry:
    """Registry that makes each unsupported chain operation explicit."""

    def __init__(self, snapshots: Iterable[CapabilitySnapshot] = ()):
        self._snapshots: dict[str, CapabilitySnapshot] = {}
        for snapshot in snapshots:
            key = snapshot.chain.upper()
            if key in self._snapshots:
                raise ValueError(f"Capability snapshot already registered for {snapshot.chain}.")
            self._snapshots[key] = snapshot

    def get(self, chain: str) -> CapabilitySnapshot:
        snapshot = self._snapshots.get(chain.upper())
        if snapshot is None:
            raise ChainCapabilityUnavailable(chain=chain, capability="chain")
        return snapshot

    def require(self, chain: str, capability: Capability) -> CapabilitySnapshot:
        snapshot = self.get(chain)
        if not snapshot.supports(capability):
            raise ChainCapabilityUnavailable(chain=chain, capability=capability)
        return snapshot
