"""Narrow chain observation boundary used only during transaction execution."""

from __future__ import annotations

from typing import Any


class ExecutionObserver:
    """Delegate execution-critical reads to the configured chain adapter.

    This class intentionally contains no transport or fallback logic. It keeps
    balances, prices, and explorer history out of the signing/broadcast path.
    """

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def adapter(self, chain: str) -> Any:
        return (
            self.registry.get_adapter(chain)
            if hasattr(self.registry, "get_adapter")
            else self.registry.get(chain)
        )

    async def get_allowance(self, chain: str, token: Any, owner: str, spender: str) -> str:
        return await self.adapter(chain).get_allowance(token, owner, spender)

    async def get_transaction_receipt(self, chain: str, tx_hash: str) -> dict[str, Any] | None:
        return await self.adapter(chain).get_transaction_receipt(tx_hash)

    async def get_transaction(self, chain: str, tx_hash: str) -> dict[str, Any] | None:
        return await self.adapter(chain).get_transaction(tx_hash)

    async def get_transaction_count(self, chain: str, address: str, block: str = "pending") -> int:
        return await self.adapter(chain).get_transaction_count(address, block)

    async def estimate_fee(self, chain: str, **kwargs: Any) -> Any:
        return await self.adapter(chain).estimate_fee(**kwargs)

    async def get_transaction_status(self, chain: str, tx_hash: str) -> Any:
        return await self.adapter(chain).get_transaction_status(tx_hash)
