"""TRON HTTP API adapter: reads, unsigned transfers, and TRC-20 approvals."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wallet_agent.domain.addresses import valid_tron_address, validate_address
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
    encode_evm_uint256_word,
    encode_tron_address_word,
    http_call,
    normalize_raw_integer,
    quantity,
    token_balance,
)

_valid_tron = valid_tron_address
_DEFAULT_FEE_LIMIT = 100_000_000  # 100 TRX ceiling for smart-contract calls.
_APPROVE_SELECTOR = "095ea7b3"
_TRANSFER_SELECTOR = "a9059cbb"
_ALLOWANCE_SELECTOR = "dd62ed3e"


class TronChainAdapter:
    #: Account model: calldata lives in a protobuf payload returned by the node.
    transaction_model = "account"

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
        fee_limit: int = _DEFAULT_FEE_LIMIT,
    ) -> None:
        self.transport = transport or http_transport or http
        if self.transport is None:
            raise ValueError("an injected TRON HTTP transport is required")
        self.chain, self.chain_id = chain.upper(), chain_id
        self.native_asset = Asset(
            chain=self.chain, chain_id=chain_id, symbol=native_symbol, decimals=native_decimals
        )
        self.token_assets = token_assets or []
        self.fee_limit = int(fee_limit)

    @property
    def capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(chain=self.chain, available=frozenset(Capability))

    async def validate_address(self, address: str) -> bool:
        return valid_tron_address(address)

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

    def _token_contract_address(self, token: Asset) -> str:
        if not token.address:
            raise ValueError("TRC-20 contract address is required")
        return validate_address(self.chain, token.address)

    @staticmethod
    def _calldata(selector: str, *words: str) -> str:
        return build_erc20_calldata(selector, *words).removeprefix("0x")

    def _unsigned_from_payload(
        self,
        payload: Any,
        *,
        action: str,
        owner: str,
        to: str,
        amount_raw: str,
        extra: Mapping[str, str] | None = None,
    ) -> UnsignedTransaction:
        """Wrap a node-built TRON transaction; ``raw_data_hex`` is what gets signed."""
        if isinstance(payload, Mapping) and isinstance(payload.get("transaction"), Mapping):
            payload = payload["transaction"]
        if not isinstance(payload, Mapping) or not payload.get("raw_data_hex"):
            raise ValueError("TRON node did not return an unsigned transaction")
        tx_id = str(payload.get("txID") or "")
        display = {
            "action": action,
            "from": owner,
            "to": to,
            "amount_raw": amount_raw,
            "tx_id": tx_id,
        }
        display.update(extra or {})
        return UnsignedTransaction(
            chain=self.chain,
            chain_id=self.chain_id,
            to=to,
            data=str(payload["raw_data_hex"]),
            value=amount_raw,
            provider_reference=tx_id or None,
            display=display,
        )

    async def _trigger_smart_contract(
        self, *, owner: str, contract: str, calldata: str, call_value: str = "0"
    ) -> Any:
        return await http_call(
            self.transport,
            "post",
            "/wallet/triggersmartcontract",
            json={
                "owner_address": owner,
                "contract_address": contract,
                "data": calldata,
                "call_value": int(call_value),
                "fee_limit": self.fee_limit,
                "visible": True,
            },
        )

    async def build_native_transfer(
        self, *, from_address: str, to_address: str, amount_raw: str
    ) -> UnsignedTransaction:
        owner = validate_address(self.chain, from_address)
        recipient = validate_address(self.chain, to_address)
        amount = normalize_raw_integer(amount_raw)
        payload = await http_call(
            self.transport,
            "post",
            "/wallet/createtransaction",
            json={
                "owner_address": owner,
                "to_address": recipient,
                "amount": int(amount),
                "visible": True,
            },
        )
        return self._unsigned_from_payload(
            payload, action="native_transfer", owner=owner, to=recipient, amount_raw=amount
        )

    async def build_erc20_transfer(
        self, *, token: Asset, from_address: str, to_address: str, amount_raw: str
    ) -> UnsignedTransaction:
        contract = self._token_contract_address(token)
        owner = validate_address(self.chain, from_address)
        recipient = validate_address(self.chain, to_address)
        amount = normalize_raw_integer(amount_raw)
        payload = await self._trigger_smart_contract(
            owner=owner,
            contract=contract,
            calldata=self._calldata(
                _TRANSFER_SELECTOR,
                encode_tron_address_word(recipient),
                encode_evm_uint256_word(amount),
            ),
        )
        return self._unsigned_from_payload(
            payload,
            action="trc20_transfer",
            owner=owner,
            to=recipient,
            amount_raw=amount,
            extra={"token": token.symbol, "contract": contract},
        )

    async def build_erc20_approve(
        self, *, token: Asset, owner: str, spender: str, amount_raw: str
    ) -> UnsignedTransaction:
        contract = self._token_contract_address(token)
        owner_address = validate_address(self.chain, owner)
        spender_address = validate_address(self.chain, spender)
        amount = normalize_raw_integer(amount_raw)
        payload = await self._trigger_smart_contract(
            owner=owner_address,
            contract=contract,
            calldata=self._calldata(
                _APPROVE_SELECTOR,
                encode_tron_address_word(spender_address),
                encode_evm_uint256_word(amount),
            ),
        )
        return self._unsigned_from_payload(
            payload,
            action="trc20_approve",
            owner=owner_address,
            to=contract,
            amount_raw=amount,
            extra={"token": token.symbol, "spender": spender_address},
        )

    async def get_allowance(self, token: Asset, owner: str, spender: str) -> str:
        contract = self._token_contract_address(token)
        owner_address = validate_address(self.chain, owner)
        payload = await http_call(
            self.transport,
            "post",
            "/wallet/triggerconstantcontract",
            json={
                "owner_address": owner_address,
                "contract_address": contract,
                "data": self._calldata(
                    _ALLOWANCE_SELECTOR,
                    encode_tron_address_word(owner_address),
                    encode_tron_address_word(spender),
                ),
                "visible": True,
            },
        )
        results = payload.get("constant_result") if isinstance(payload, Mapping) else None
        if not results:
            return "0"
        try:
            return str(int(str(results[0] or "0"), 16))
        except (TypeError, ValueError):
            return "0"

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
