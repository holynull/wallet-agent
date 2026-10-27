"""Typed chain capability declarations and lookup registry."""

import re
from collections.abc import Iterable
from enum import Enum

from eth_hash.auto import keccak
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

# Trust Wallet renamed BSC's directory from ``binance`` to ``smartchain``. The
# chain info badge still exists under ``binance``, but per-token asset logos live
# under ``smartchain`` (the ``binance/assets/...`` paths 404, e.g. BSC USDT).
_TOKEN_LOGO_SLUGS = {
    "BSC": "smartchain",
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


def _evm_checksum_address(address: str) -> str:
    """EIP-55 checksummed form of an EVM hex address.

    Trust Wallet's per-token asset directories are keyed by the checksummed
    contract address, so the lowercase copy of an address 404s and the App
    renders a broken image. Non-EVM identifiers (e.g. TRON's base58 ``T...``
    contract addresses) are left untouched.
    """
    text = str(address or "")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", text):
        return text
    body = text[2:].lower()
    digest = keccak(body.encode()).hex()
    return "0x" + "".join(
        ch.upper() if int(digest[i], 16) >= 8 else ch
        for i, ch in enumerate(body)
    )


def asset_logo_url(chain: str, contract_address: str | None) -> str | None:
    """Trust Wallet logo URL for a contract-backed token (ERC20/TRC20 etc).

    Returns ``None`` when the chain slug or contract address is unknown, so the
    App renders its own placeholder instead of a broken image.
    """
    text = str(contract_address or "").strip()
    if not text:
        return None
    # EVM token directories are keyed by a full 0x + 40 hex contract address; a
    # placeholder/partial address would only produce a 404 image URL.
    if text.startswith("0x") and len(text) != 42:
        return None
    canonical = canonical_chain(chain)
    slug = _TOKEN_LOGO_SLUGS.get(canonical) or _CHAIN_LOGO_SLUGS.get(canonical)
    if not slug:
        return None
    contract = _evm_checksum_address(text)
    return f"{_CHAIN_LOGO_BASE}/{slug}/assets/{contract}/logo.png"


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
