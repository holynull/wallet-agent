"""Typed chain capability declarations and lookup registry."""

from collections.abc import Iterable
from enum import Enum

from pydantic import Field

from .errors import ChainCapabilityUnavailable
from .models import DomainModel


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
