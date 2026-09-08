"""EVM JSON-RPC read-only adapter."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from wallet_agent.domain.chains import Capability, CapabilitySnapshot
from wallet_agent.domain.models import (
    Asset,
    FeeEstimate,
    TokenBalance,
    TransactionRecord,
    TransactionStatus,
    UnsignedTransaction,
)

from ._common import (
    build_erc20_calldata,
    encode_evm_address_word,
    encode_evm_uint256_word,
    normalize_raw_integer,
    parse_status,
    quantity,
    receipt_success,
    rpc_call,
    token_balance,
    validate_evm_address,
)

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class EVMChainAdapter:
    def __init__(
        self,
        transport: Any = None,
        *,
        rpc_transport: Any = None,
        rpc: Any = None,
        chain: str = "EVM",
        chain_id: int | str | None = None,
        native_symbol: str = "ETH",
        native_decimals: int = 18,
        token_assets: list[Asset] | None = None,
    ) -> None:
        self.transport = transport or rpc_transport or rpc
        if self.transport is None:
            raise ValueError("an injected EVM RPC transport is required")
        self.chain = chain.upper()
        self.chain_id = chain_id
        self.native_asset = Asset(
            chain=self.chain, chain_id=chain_id, symbol=native_symbol, decimals=native_decimals
        )
        self.token_assets = token_assets or []

    @property
    def capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            chain=self.chain,
            available=frozenset(Capability),
        )

    async def validate_address(self, address: str) -> bool:
        return bool(_ADDRESS.fullmatch(address.strip()))

    async def get_token_balance(self, asset: Asset, owner: str) -> TokenBalance:
        if not asset.address:
            raise ValueError("token address is required")
        owner_address = validate_evm_address(owner)
        raw = quantity(
            await rpc_call(
                self.transport,
                "eth_call",
                [
                    {
                        "to": asset.address,
                        "data": build_erc20_calldata(
                            "70a08231", encode_evm_address_word(owner_address)
                        ),
                    },
                    "latest",
                ],
            )
        )
        return token_balance(asset, raw)

    async def get_native_balance(self, address: str) -> TokenBalance:
        raw = quantity(await rpc_call(self.transport, "eth_getBalance", [address, "latest"]))
        return token_balance(self.native_asset, raw)

    async def get_token_balances(self, address: str) -> list[TokenBalance]:
        # ERC-20 balanceOf calls are deterministic and work with any standard EVM node.
        balances: list[TokenBalance] = []
        for asset in self.token_assets:
            if not asset.address:
                continue
            amount = quantity((await self.get_token_balance(asset, address)).amount_raw)
            if amount:
                balances.append(token_balance(asset, amount))
        return balances

    async def get_allowance(self, token: Asset, owner: str, spender: str) -> str:
        if not token.address:
            raise ValueError("token address is required")
        owner_address = validate_evm_address(owner)
        spender_address = validate_evm_address(spender)
        raw = await rpc_call(
            self.transport,
            "eth_call",
            [
                {
                    "to": token.address,
                    "data": build_erc20_calldata(
                        "dd62ed3e",
                        encode_evm_address_word(owner_address),
                        encode_evm_address_word(spender_address),
                    ),
                },
                "latest",
            ],
        )
        return normalize_raw_integer(quantity(raw))

    async def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        receipt = await rpc_call(self.transport, "eth_getTransactionReceipt", [tx_hash])
        return receipt if isinstance(receipt, dict) else None

    async def get_transaction_history(
        self, address: str, *, limit: int = 20
    ) -> list[TransactionRecord]:
        try:
            value = await rpc_call(self.transport, "wallet_getTransactionHistory", [address, limit])
        except Exception:
            value = []
        if isinstance(value, dict):
            value = value.get("transactions", value.get("items", []))
        if not isinstance(value, list):
            return []
        return [self._record(item) for item in value if isinstance(item, dict)][:limit]

    async def estimate_fee(self, *, to: str | None = None, data: str | None = None) -> FeeEstimate:
        tx: dict[str, str] = {}
        if to:
            tx["to"] = to
        if data:
            tx["data"] = data
        gas = quantity(await rpc_call(self.transport, "eth_estimateGas", [tx]))
        gas_price = quantity(await rpc_call(self.transport, "eth_gasPrice", []))
        total = gas * gas_price
        return FeeEstimate(
            chain=self.chain,
            chain_id=self.chain_id,
            asset=self.native_asset,
            amount=token_balance(self.native_asset, total).amount,
            amount_raw=str(total),
            gas_limit=str(gas),
        )

    async def get_transaction_status(self, tx_hash: str) -> TransactionStatus:
        receipt = await self.get_transaction_receipt(tx_hash)
        if receipt is None:
            return TransactionStatus.PENDING
        status = receipt_success(receipt)
        if status is None:
            status = parse_status(receipt.get("status"))
            return TransactionStatus(status)
        if status:
            return TransactionStatus.CONFIRMED
        return TransactionStatus.FAILED

    def build_native_transfer(
        self, *, from_address: str, to_address: str, amount_raw: str
    ) -> UnsignedTransaction:
        validate_evm_address(from_address)
        to = validate_evm_address(to_address)
        value = normalize_raw_integer(amount_raw)
        return UnsignedTransaction(
            chain=self.chain,
            chain_id=self.chain_id,
            to=to,
            data="0x",
            value=value,
            display={
                "action": "native_transfer",
                "from": from_address,
                "to": to,
                "amount_raw": value,
            },
        )

    def build_erc20_transfer(
        self, *, token: Asset, from_address: str, to_address: str, amount_raw: str
    ) -> UnsignedTransaction:
        if not token.address:
            raise ValueError("token address is required")
        validate_evm_address(from_address)
        to = validate_evm_address(to_address)
        value = normalize_raw_integer(amount_raw)
        return UnsignedTransaction(
            chain=self.chain,
            chain_id=self.chain_id,
            to=token.address,
            data=build_erc20_calldata(
                "a9059cbb", encode_evm_address_word(to), encode_evm_uint256_word(value)
            ),
            value="0",
            display={
                "action": "erc20_transfer",
                "token": token.symbol,
                "from": from_address,
                "to": to,
                "amount_raw": value,
            },
        )

    def build_erc20_approve(
        self, *, token: Asset, owner: str, spender: str, amount_raw: str
    ) -> UnsignedTransaction:
        if not token.address:
            raise ValueError("token address is required")
        validate_evm_address(owner)
        spender_address = validate_evm_address(spender)
        value = normalize_raw_integer(amount_raw)
        return UnsignedTransaction(
            chain=self.chain,
            chain_id=self.chain_id,
            to=token.address,
            data=build_erc20_calldata(
                "095ea7b3", encode_evm_address_word(spender_address), encode_evm_uint256_word(value)
            ),
            value="0",
            display={
                "action": "erc20_approve",
                "token": token.symbol,
                "owner": owner,
                "spender": spender_address,
                "amount_raw": value,
            },
        )

    def _record(self, item: dict[str, Any]) -> TransactionRecord:
        status = parse_status(item.get("status") or item.get("txreceipt_status"))
        if status == "unknown":
            status = "confirmed" if item.get("blockNumber") is not None else "pending"
        confirmed_at = None
        if item.get("timestamp"):
            try:
                confirmed_at = datetime.fromtimestamp(int(item["timestamp"]), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                pass
        return TransactionRecord(
            chain=self.chain,
            chain_id=self.chain_id,
            tx_hash=str(item.get("hash") or item.get("txHash") or ""),
            status=TransactionStatus(status),
            from_address=item.get("from"),
            to_address=item.get("to"),
            value=str(item["value"]) if item.get("value") is not None else None,
            block_number=quantity(item["blockNumber"])
            if item.get("blockNumber") is not None
            else None,
            confirmed_at=confirmed_at,
        )


EVMAdapter = EVMChainAdapter
EVMChain = EVMChainAdapter
