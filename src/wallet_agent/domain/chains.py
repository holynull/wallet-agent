"""Typed chain capability declarations and lookup registry."""

from collections.abc import Iterable
from enum import Enum

from pydantic import Field

from .errors import ChainCapabilityUnavailable
from .models import DomainModel
from .normalization import canonical_chain

_NATIVE_DECIMALS = {
    "BTC": 8,
    "TRON": 6,
    "TRX": 6,
    "SOLANA": 9,
    "SOL": 9,
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
