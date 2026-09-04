"""Explicit adapters for chain families not implemented in this release."""

from typing import Any

from wallet_agent.domain.chains import CapabilitySnapshot
from wallet_agent.domain.errors import ChainCapabilityUnavailable


class UnsupportedChainAdapter:
    def __init__(self, chain: str, **_: Any) -> None:
        self.chain = chain.upper()

    @property
    def capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(chain=self.chain)

    def _raise(self, capability: str) -> None:
        raise ChainCapabilityUnavailable(self.chain, capability)

    async def validate_address(self, address: str) -> bool:
        self._raise("address_validation")

    async def get_native_balance(self, address: str) -> Any:
        self._raise("balances")

    async def get_token_balances(self, address: str) -> Any:
        self._raise("balances")

    async def get_transaction_history(self, address: str, *, limit: int = 20) -> Any:
        self._raise("history")

    async def estimate_fee(self, *, to: str | None = None, data: str | None = None) -> Any:
        self._raise("fees")

    async def get_transaction_status(self, tx_hash: str) -> Any:
        self._raise("transaction_status")
