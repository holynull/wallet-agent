"""Solana JSON-RPC read-only adapter."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from wallet_agent.domain.chains import Capability, CapabilitySnapshot
from wallet_agent.domain.models import (
    Asset,
    FeeEstimate,
    TokenBalance,
    TransactionRecord,
    TransactionStatus,
)

from ._common import quantity, rpc_call, token_balance

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_bytes(value: str) -> bytes:
    n = 0
    for char in value:
        n = n * 58 + _ALPHABET.index(char)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + raw


class SolanaChainAdapter:
    def __init__(
        self,
        transport: Any = None,
        *,
        http_transport: Any = None,
        rpc_transport: Any = None,
        chain: str = "SOLANA",
        chain_id: str | int | None = None,
        native_symbol: str = "SOL",
        native_decimals: int = 9,
    ) -> None:
        self.transport = transport or http_transport or rpc_transport
        if self.transport is None:
            raise ValueError("an injected Solana HTTP transport is required")
        self.chain, self.chain_id = chain.upper(), chain_id
        self.native_asset = Asset(
            chain=self.chain, chain_id=chain_id, symbol=native_symbol, decimals=native_decimals
        )

    @property
    def capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(chain=self.chain, available=frozenset(Capability))

    async def validate_address(self, address: str) -> bool:
        try:
            return len(_base58_bytes(address.strip())) == 32
        except (ValueError, IndexError):
            return False

    async def get_native_balance(self, address: str) -> TokenBalance:
        result = await rpc_call(self.transport, "getBalance", [address])
        value = result.get("value", 0) if isinstance(result, dict) else result
        return token_balance(self.native_asset, quantity(value))

    async def get_token_balances(self, address: str) -> list[TokenBalance]:
        result = await rpc_call(
            self.transport,
            "getTokenAccountsByOwner",
            [
                address,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"},
            ],
        )
        rows = (result or {}).get("value", []) if isinstance(result, dict) else []
        balances: list[TokenBalance] = []
        for row in rows:
            info = ((row.get("account", {}).get("data", {}) or {}).get("parsed", {}) or {}).get(
                "info", {}
            ) or {}
            token = info.get("tokenAmount", {})
            mint = info.get("mint")
            decimals = int(token.get("decimals", 0))
            asset = Asset(
                chain=self.chain,
                chain_id=self.chain_id,
                symbol=str(mint),
                decimals=decimals,
                address=mint,
            )
            balances.append(token_balance(asset, token.get("amount", 0)))
        return balances

    async def get_transaction_history(
        self, address: str, *, limit: int = 20
    ) -> list[TransactionRecord]:
        rows = await rpc_call(
            self.transport, "getSignaturesForAddress", [address, {"limit": limit}]
        )
        return [
            TransactionRecord(
                chain=self.chain,
                chain_id=self.chain_id,
                tx_hash=str(x.get("signature", "")),
                status=TransactionStatus.FAILED if x.get("err") else TransactionStatus.CONFIRMED,
                block_number=x.get("slot"),
            )
            for x in (rows or [])
            if isinstance(x, dict)
        ][:limit]

    async def estimate_fee(self, *, to: str | None = None, data: str | None = None) -> FeeEstimate:
        if data:
            result = await rpc_call(self.transport, "getFeeForMessage", [data])
            value = result.get("value", 0) if isinstance(result, dict) else result
            fee = quantity(value)
        else:
            rows = await rpc_call(self.transport, "getRecentPrioritizationFees", [])
            fee = max(
                (
                    quantity(x.get("prioritizationFee", 0))
                    for x in (rows or [])
                    if isinstance(x, dict)
                ),
                default=0,
            )
        return FeeEstimate(
            chain=self.chain,
            chain_id=self.chain_id,
            asset=self.native_asset,
            amount=Decimal(fee) / (Decimal(10) ** 9),
            amount_raw=str(fee),
        )

    async def get_transaction_status(self, tx_hash: str) -> TransactionStatus:
        result = await rpc_call(self.transport, "getSignatureStatuses", [[tx_hash]])
        value = (result or {}).get("value", [None])[0] if isinstance(result, dict) else None
        if value is None:
            return TransactionStatus.UNKNOWN
        if value.get("err"):
            return TransactionStatus.FAILED
        return (
            TransactionStatus.CONFIRMED
            if value.get("confirmationStatus") in {"confirmed", "finalized"}
            else TransactionStatus.PENDING
        )


SolanaAdapter = SolanaChainAdapter
SolanaChain = SolanaChainAdapter
