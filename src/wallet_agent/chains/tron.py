"""TRON HTTP API read-only adapter."""

from __future__ import annotations

import hashlib
from typing import Any

from wallet_agent.domain.chains import Capability, CapabilitySnapshot
from wallet_agent.domain.models import (
    Asset,
    FeeEstimate,
    TokenBalance,
    TransactionRecord,
    TransactionStatus,
)

from ._common import http_call, quantity, token_balance

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(value: str) -> bytes:
    number = 0
    for char in value:
        number = number * 58 + _ALPHABET.index(char)
    return number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b"\0"


def _valid_tron(address: str) -> bool:
    try:
        raw = _base58_decode(address)
        if len(raw) != 25 or raw[0] != 0x41:
            return False
        return hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] == raw[-4:]
    except (ValueError, IndexError):
        return False


class TronChainAdapter:
    def __init__(
        self,
        transport: Any = None,
        *,
        http_transport: Any = None,
        http: Any = None,
        chain: str = "TRON",
        chain_id: str | int | None = None,
        native_symbol: str = "TRX",
        native_decimals: int = 6,
        token_assets: list[Asset] | None = None,
    ) -> None:
        self.transport = transport or http_transport or http
        if self.transport is None:
            raise ValueError("an injected TRON HTTP transport is required")
        self.chain, self.chain_id = chain.upper(), chain_id
        self.native_asset = Asset(
            chain=self.chain, chain_id=chain_id, symbol=native_symbol, decimals=native_decimals
        )
        self.token_assets = token_assets or []

    @property
    def capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(chain=self.chain, available=frozenset(Capability))

    async def validate_address(self, address: str) -> bool:
        return _valid_tron(address.strip())

    async def get_native_balance(self, address: str) -> TokenBalance:
        data = await http_call(self.transport, "get", f"/v1/accounts/{address}")
        account = (data.get("data") or [{}])[0] if isinstance(data, dict) else data
        if not isinstance(account, dict):
            account = {}
        return token_balance(self.native_asset, account.get("balance", 0))

    async def get_token_balances(self, address: str) -> list[TokenBalance]:
        data = await http_call(self.transport, "get", f"/v1/accounts/{address}")
        account = (data.get("data") or [{}])[0] if isinstance(data, dict) else data
        if not isinstance(account, dict):
            account = {}
        entries = account.get("trc20", []) if isinstance(account, dict) else []
        result: list[TokenBalance] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            contract, raw = next(iter(entry.items()), (None, None))
            info = entry.get("token_info", {}) if isinstance(entry.get("token_info"), dict) else {}
            asset = next((a for a in self.token_assets if a.address == contract), None)
            if asset is None:
                asset = Asset(
                    chain=self.chain,
                    chain_id=self.chain_id,
                    symbol=str(info.get("symbol", "TOKEN")),
                    decimals=int(info.get("decimals", 6)),
                    address=str(contract) if contract else None,
                    name=info.get("name"),
                )
            result.append(token_balance(asset, raw or 0))
        return result

    async def get_transaction_history(
        self, address: str, *, limit: int = 20
    ) -> list[TransactionRecord]:
        data = await http_call(
            self.transport, "get", f"/v1/accounts/{address}/transactions", params={"limit": limit}
        )
        rows = data.get("data", []) if isinstance(data, dict) else data
        return [self._record(x) for x in rows if isinstance(x, dict)][:limit]

    async def estimate_fee(self, *, to: str | None = None, data: str | None = None) -> FeeEstimate:
        params = await http_call(self.transport, "get", "/wallet/getchainparameters")
        fee = 0
        for item in params.get("chainParameter", []) if isinstance(params, dict) else []:
            if item.get("key") in {"getTransactionFee", "energyFee"}:
                fee = quantity(item.get("value", 0))
                break
        return FeeEstimate(
            chain=self.chain,
            chain_id=self.chain_id,
            asset=self.native_asset,
            amount=token_balance(self.native_asset, fee).amount,
            amount_raw=str(fee),
        )

    async def get_transaction_status(self, tx_hash: str) -> TransactionStatus:
        data = await http_call(self.transport, "get", f"/wallet/gettransactionbyid/{tx_hash}")
        if not data:
            return TransactionStatus.UNKNOWN
        ret = (data.get("ret") or [{}])[0] if isinstance(data, dict) else {}
        contract = str(ret.get("contractRet", "")).upper()
        if contract in {"SUCCESS", "SUCESS"}:
            return TransactionStatus.CONFIRMED
        if contract in {"FAILED", "REVERT"}:
            return TransactionStatus.FAILED
        return TransactionStatus.PENDING

    def _record(self, item: dict[str, Any]) -> TransactionRecord:
        contract = ((item.get("ret") or [{}])[0] or {}).get("contractRet", "")
        status = (
            TransactionStatus.CONFIRMED
            if str(contract).upper() in {"SUCCESS", "SUCESS"}
            else TransactionStatus.FAILED
            if contract
            else TransactionStatus.PENDING
        )
        return TransactionRecord(
            chain=self.chain,
            chain_id=self.chain_id,
            tx_hash=str(item.get("txID", "")),
            status=status,
            block_number=item.get("block", item.get("blockNumber")),
        )


TronAdapter = TronChainAdapter
TronChain = TronChainAdapter
