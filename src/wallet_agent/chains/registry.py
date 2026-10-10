"""Chain adapter registry and default chain-family wiring."""

from __future__ import annotations

from typing import Any

from wallet_agent.domain.errors import ChainCapabilityUnavailable

from .btc import BitcoinChainAdapter
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

    def items(self):
        return self._adapters.items()

    def require(self, chain: str, capability: Any) -> Any:
        adapter = self.get(chain)
        if not adapter.capabilities.supports(capability):
            raise ChainCapabilityUnavailable(chain, capability)
        return adapter


ChainRegistry = ChainAdapterRegistry


def _esplora_host(url: str) -> str:
    """Normalize an Esplora endpoint to its host.

    Operators paste either ``https://blockstream.info`` or its documented API
    base ``https://blockstream.info/api``; the adapter already owns the ``/api``
    path, so strip a trailing segment to avoid a doubled prefix.
    """
    trimmed = str(url).rstrip("/")
    return trimmed[: -len("/api")] if trimmed.endswith("/api") else trimmed


def build_default_registry(
    *,
    evm_transport: Any = None,
    tron_transport: Any = None,
    solana_transport: Any = None,
    btc_transport: Any = None,
    rpc_urls: dict[str, str | list[str]] | None = None,
    rpc_timeout_seconds: float = 10,
    rpc_max_attempts: int = 2,
    include_stubs: bool = True,
) -> ChainAdapterRegistry:
    adapters: dict[str, Any] = {}
    if evm_transport is not None:
        evm = EVMChainAdapter(evm_transport)
        adapters["EVM"] = evm
        for alias in ("ETH", "BSC", "BASE", "ARB", "ARBITRUM", "POLYGON", "OPTIMISM", "OP"):
            adapters.setdefault(alias, evm)
    if tron_transport is not None:
        adapters["TRON"] = TronChainAdapter(tron_transport)
    if btc_transport is not None:
        adapters["BTC"] = BitcoinChainAdapter(btc_transport)
    if solana_transport is not None:
        adapters["SOLANA"] = SolanaChainAdapter(solana_transport)
    if rpc_urls:
        from .transports import FailoverHttpTransport, FailoverJsonRpcTransport

        def urls(chain: str) -> list[str]:
            value = rpc_urls.get(chain) or rpc_urls.get(chain.upper())
            if isinstance(value, str):
                return [value]
            return list(value or [])

        chain_ids = {
            "ETH": 1,
            "BSC": 56,
            "BASE": 8453,
            "ARBITRUM": 42161,
            "OPTIMISM": 10,
            "POLYGON": 137,
        }
        native_symbols = {
            "ETH": "ETH",
            "BSC": "BNB",
            "BASE": "ETH",
            "ARBITRUM": "ETH",
            "OPTIMISM": "ETH",
            "POLYGON": "MATIC",
        }
        for chain, chain_id in chain_ids.items():
            if chain not in adapters and urls(chain):
                adapters[chain] = EVMChainAdapter(
                    FailoverJsonRpcTransport(
                        urls(chain),
                        timeout_seconds=rpc_timeout_seconds,
                        max_attempts=rpc_max_attempts,
                    ),
                    chain=chain,
                    chain_id=chain_id,
                    native_symbol=native_symbols[chain],
                )
        if "TRON" not in adapters and urls("TRON"):
            adapters["TRON"] = TronChainAdapter(
                FailoverHttpTransport(urls("TRON"), timeout_seconds=rpc_timeout_seconds)
            )
        if "BTC" not in adapters and urls("BTC"):
            adapters["BTC"] = BitcoinChainAdapter(
                FailoverHttpTransport(
                    [_esplora_host(url) for url in urls("BTC")],
                    timeout_seconds=rpc_timeout_seconds,
                )
            )
        if "SOLANA" not in adapters and urls("SOLANA"):
            adapters["SOLANA"] = SolanaChainAdapter(
                FailoverJsonRpcTransport(
                    urls("SOLANA"),
                    timeout_seconds=rpc_timeout_seconds,
                    max_attempts=rpc_max_attempts,
                )
            )
    if include_stubs:
        for chain in ("SUI", "APTOS", "XRP", "XLM", "WAVES"):
            adapters.setdefault(chain, UnsupportedChainAdapter(chain))
    return ChainAdapterRegistry(adapters)
