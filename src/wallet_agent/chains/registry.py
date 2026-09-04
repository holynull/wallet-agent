"""Chain adapter registry and default chain-family wiring."""

from __future__ import annotations

from typing import Any

from wallet_agent.domain.errors import ChainCapabilityUnavailable

from .evm import EVMChainAdapter
from .solana import SolanaChainAdapter
from .stubs import UnsupportedChainAdapter
from .tron import TronChainAdapter


class ChainAdapterRegistry:
    def __init__(self, adapters: dict[str, Any] | None = None) -> None:
        self._adapters = {str(k).upper(): v for k, v in (adapters or {}).items()}

    def register(self, chain: str, adapter: Any) -> None:
        self._adapters[chain.upper()] = adapter

    def get(self, chain: str) -> Any:
        try:
            return self._adapters[chain.upper()]
        except KeyError as exc:
            raise ChainCapabilityUnavailable(chain, "chain") from exc

    get_adapter = get

    def require(self, chain: str, capability: Any) -> Any:
        adapter = self.get(chain)
        if not adapter.capabilities.supports(capability):
            raise ChainCapabilityUnavailable(chain, capability)
        return adapter


ChainRegistry = ChainAdapterRegistry


def build_default_registry(
    *,
    evm_transport: Any = None,
    tron_transport: Any = None,
    solana_transport: Any = None,
    rpc_urls: dict[str, str] | None = None,
    include_stubs: bool = True,
) -> ChainAdapterRegistry:
    del rpc_urls  # transports are intentionally injected by the application boundary
    adapters: dict[str, Any] = {}
    if evm_transport is not None:
        evm = EVMChainAdapter(evm_transport)
        adapters["EVM"] = evm
        for alias in ("ETH", "BSC", "BASE", "ARB", "ARBITRUM", "POLYGON", "OPTIMISM", "OP"):
            adapters.setdefault(alias, evm)
    if tron_transport is not None:
        adapters["TRON"] = TronChainAdapter(tron_transport)
    if solana_transport is not None:
        adapters["SOLANA"] = SolanaChainAdapter(solana_transport)
    if include_stubs:
        for chain in ("SUI", "APTOS", "XRP", "XLM", "WAVES"):
            adapters.setdefault(chain, UnsupportedChainAdapter(chain))
    return ChainAdapterRegistry(adapters)
