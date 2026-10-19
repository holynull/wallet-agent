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
    receipt_success,
    token_balance,
)

_valid_tron = valid_tron_address
_DEFAULT_FEE_LIMIT = 100_000_000  # 100 TRX ceiling for smart-contract calls.
#: Conservative TRX-denominated (sun) floor for a smart-contract call when the
#: node cannot simulate it. A USDT transfer to a fresh recipient burns roughly
#: 10-30 TRX of energy (dynamic pricing included); the 0.001 TRX native-transfer
#: bandwidth fee is never a realistic contract-call estimate, and using it made
#: the gas-balance preflight pass while the real transfer later ran out of energy.
_TRC20_FEE_FLOOR_SUN = 12_000_000
_APPROVE_SELECTOR = "095ea7b3"
_TRANSFER_SELECTOR = "a9059cbb"
_ALLOWANCE_SELECTOR = "dd62ed3e"


def _wei_value(value: Any) -> int:
    """Normalize a native-amount string (decimal or ``0x`` hex) to an integer."""
    text = str(value or "0").strip()
    if not text:
        return 0
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def _chain_parameter_value(params: Any, keys: set[str]) -> int:
    """Read the first TRON chain parameter whose key is in ``keys``, in sun."""
    for item in params if isinstance(params, list) else []:
        if not isinstance(item, Mapping) or str(item.get("key") or "") not in keys:
            continue
        return _wei_value(item.get("value") or 0)
    return 0


def _tron_contract_fees(payload: Any, energy_price: int) -> tuple[int, int]:
    """Return ``(energy_fee_sun, net_fee_sun)`` a simulated contract call burns.

    The node prices the consumed energy in ``energy_fee`` / ``energy_fee_total``
    (sun, including the dynamic-energy penalty) when it charged for energy; when
    absent, recompute from ``energy_used_total`` and the chain's energy price.
    ``net_fee`` is the bandwidth fee.
    """
    if not isinstance(payload, Mapping):
        return 0, 0
    net_fee = _wei_value(payload.get("net_fee"))
    energy_fee = _wei_value(payload.get("energy_fee_total") or payload.get("energy_fee"))
    if not energy_fee:
        energy_used = _wei_value(payload.get("energy_used_total"))
        if not energy_used:
            energy_used = _wei_value(payload.get("energy_used")) + _wei_value(
                payload.get("energy_penalty")
            )
        energy_fee = energy_used * max(int(energy_price or 0), 0)
    return energy_fee, net_fee


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

    async def estimate_fee(
        self,
        *,
        to: str | None = None,
        data: str | None = None,
        from_address: str | None = None,
        value: str | None = None,
        contract: str | None = None,
        calldata: str | None = None,
    ) -> FeeEstimate:
        """Estimate the TRX a transaction will burn.

        A native TRX transfer burns only the bandwidth base fee
        (``getTransactionFee``, ~0.001 TRX). A smart-contract call (TRC-20
        transfer/approve, swap) additionally burns energy, which dominates the
        cost: without staked energy a USDT transfer to a fresh recipient consumes
        tens of TRX of energy. When a contract and calldata are known, simulate
        the call through ``triggersmartcontract`` and price the returned energy so
        the confirmation card and the gas-balance preflight see a realistic number
        instead of the native-transfer base fee.
        """
        params = await http_call(self.transport, "get", "/wallet/getchainparameters")
        chain_parameters = params.get("chainParameter", []) if isinstance(params, dict) else []
        bandwidth_fee = _chain_parameter_value(
            chain_parameters, {"getTransactionFee", "transactionFee"}
        )
        energy_price = _chain_parameter_value(
            chain_parameters, {"getEnergyFee", "energyFee", "energy_price"}
        )

        owner = str(from_address or "").strip()
        effective_contract = str(contract or to or "").strip()
        effective_calldata = str(calldata or data or "").strip().removeprefix("0x")
        if owner and effective_contract and effective_calldata:
            # A TRC-20 transfer never sends TRX to the contract, and ``value`` on
            # the built transaction is the token amount, so simulate with a zero
            # call value. If the node cannot simulate, fall back to a conservative
            # TRC-20 floor instead of the misleading bandwidth fee.
            try:
                payload = await self._trigger_smart_contract(
                    owner=validate_address(self.chain, owner),
                    contract=validate_address(self.chain, effective_contract),
                    calldata=effective_calldata,
                    call_value="0",
                )
                energy_fee, net_fee = _tron_contract_fees(payload, energy_price)
                fee_sun = energy_fee + net_fee
                if fee_sun <= 0:
                    fee_sun = _TRC20_FEE_FLOOR_SUN
            except Exception:
                fee_sun = _TRC20_FEE_FLOOR_SUN
        else:
            fee_sun = bandwidth_fee
        return FeeEstimate(
            chain=self.chain,
            chain_id=self.chain_id,
            asset=self.native_asset,
            amount=token_balance(self.native_asset, fee_sun).amount,
            amount_raw=str(fee_sun),
        )

    async def get_transaction_status(self, tx_hash: str) -> TransactionStatus:
        data = await http_call(self.transport, "get", f"/wallet/gettransactionbyid/{tx_hash}")
        if not data:
            return TransactionStatus.UNKNOWN
        receipt = self._receipt_from_transaction(data)
        if receipt is None:
            return TransactionStatus.PENDING
        success = receipt_success(receipt)
        if success is True:
            return TransactionStatus.CONFIRMED
        if success is False:
            return TransactionStatus.FAILED
        return TransactionStatus.PENDING

    async def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        """Map a TRON node transaction onto an EVM-shaped receipt for the swap gate.

        ``/wallet/gettransactionbyid`` carries the execution outcome in
        ``ret[0].contractRet`` (``SUCCESS`` / ``FAILED`` / ``REVERT``). Return a
        ``status``-keyed mapping so ``receipt_success`` and the broadcast observer
        classify it exactly like an EVM receipt; ``None`` means the transaction
        is still pending or unknown.
        """
        data = await http_call(self.transport, "get", f"/wallet/gettransactionbyid/{tx_hash}")
        return self._receipt_from_transaction(data)

    @staticmethod
    def _receipt_from_transaction(data: Any) -> dict[str, Any] | None:
        if not isinstance(data, Mapping):
            return None
        ret = (data.get("ret") or [{}])[0] if isinstance(data.get("ret"), list) else {}
        if not isinstance(ret, Mapping):
            ret = {}
        contract = str(ret.get("contractRet") or "").upper()
        if contract in {"SUCCESS", "SUCESS"}:
            return {"status": "0x1", "contractRet": contract}
        if contract in {"FAILED", "REVERT"}:
            return {"status": "0x0", "contractRet": contract}
        return None

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
                "call_value": _wei_value(call_value),
                "fee_limit": self.fee_limit,
                "visible": True,
            },
        )

    async def build_smart_contract_call(
        self,
        *,
        from_address: str,
        contract_address: str,
        calldata: str,
        value_raw: str = "0",
    ) -> UnsignedTransaction:
        """Wrap arbitrary calldata (e.g. a provider's swap payload) as a TRON tx.

        Account-model wallets sign the node-built protobuf (``raw_data_hex``), not
        raw EVM calldata. Providers such as Bridgers return TRON swaps in EVM shape
        (``{to, data, value}``); this rebuilds that payload through
        ``/wallet/triggersmartcontract`` so the App receives a signable transaction.
        """
        owner = validate_address(self.chain, from_address)
        contract = validate_address(self.chain, contract_address)
        calldata = str(calldata or "").removeprefix("0x")
        if not calldata:
            raise ValueError("TRON contract call requires calldata")
        payload = await self._trigger_smart_contract(
            owner=owner,
            contract=contract,
            calldata=calldata,
            call_value=value_raw,
        )
        amount = str(_wei_value(value_raw)) if str(value_raw) else "0"
        return self._unsigned_from_payload(
            payload,
            action="contract_call",
            owner=owner,
            to=contract,
            amount_raw=amount,
            extra={"calldata": "0x" + calldata},
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
        calldata = self._calldata(
            _TRANSFER_SELECTOR,
            encode_tron_address_word(recipient),
            encode_evm_uint256_word(amount),
        )
        payload = await self._trigger_smart_contract(
            owner=owner, contract=contract, calldata=calldata
        )
        return self._unsigned_from_payload(
            payload,
            action="trc20_transfer",
            owner=owner,
            to=recipient,
            amount_raw=amount,
            extra={"token": token.symbol, "contract": contract, "calldata": "0x" + calldata},
        )

    async def build_erc20_approve(
        self, *, token: Asset, owner: str, spender: str, amount_raw: str
    ) -> UnsignedTransaction:
        contract = self._token_contract_address(token)
        owner_address = validate_address(self.chain, owner)
        spender_address = validate_address(self.chain, spender)
        amount = normalize_raw_integer(amount_raw)
        calldata = self._calldata(
            _APPROVE_SELECTOR,
            encode_tron_address_word(spender_address),
            encode_evm_uint256_word(amount),
        )
        payload = await self._trigger_smart_contract(
            owner=owner_address, contract=contract, calldata=calldata
        )
        return self._unsigned_from_payload(
            payload,
            action="trc20_approve",
            owner=owner_address,
            to=contract,
            amount_raw=amount,
            extra={
                "token": token.symbol,
                "spender": spender_address,
                "contract": contract,
                "calldata": "0x" + calldata,
            },
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
