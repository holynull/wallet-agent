"""Bridgers API adapter."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from wallet_agent.domain.models import (
    AllowanceRequirement,
    Asset,
    AssetQuery,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    UnsignedTransaction,
)
from wallet_agent.domain.normalization import canonical_chain, canonical_symbol

from .http import ProviderResponseError

_RETRYABLE = {"412", "413", "415", "777"}
_PERMANENT = {"906", "907", "908", "999", "1114", "1145"}


def _code(response: dict[str, Any]) -> str:
    return str(response.get("resCode", ""))


def _ensure_success(response: dict[str, Any], *, upload: bool = False) -> dict[str, Any]:
    code = _code(response)
    if code == "100":
        data = response.get("data", {})
        return data if isinstance(data, dict) else {}
    if upload and code == "414":
        data = response.get("data", {})
        return data if isinstance(data, dict) else {}
    retryable = code in _RETRYABLE
    category = "retryable" if retryable else "permanent" if code in _PERMANENT else "client"
    raise ProviderResponseError(
        code,
        str(response.get("resMsg", "Provider request failed")),
        retryable=retryable,
        category=category,
    )


def _equipment(address: str) -> str:
    return address[:32]


def _raw_decimal(value: Decimal, decimals: int) -> str:
    return str(int(value * (Decimal(10) ** decimals)))


class BridgersProvider:
    provider_name = "bridgers"

    def __init__(
        self,
        transport: Any,
        *,
        source_flag: str = "",
        source_type: str | None = None,
        spender_by_chain: dict[str, str] | None = None,
        swap_spender: str | None = None,
    ) -> None:
        self.transport = transport
        self.source_flag = source_flag
        self.source_type = source_type
        self.spender_by_chain = {
            str(k).upper(): str(v) for k, v in (spender_by_chain or {}).items()
        }
        self.swap_spender = swap_spender
        self._quotes: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_transport(cls, transport: Any, **kwargs: Any) -> "BridgersProvider":
        return cls(transport, **kwargs)

    async def list_assets(self, query: AssetQuery) -> list[Asset]:
        chain_filter = canonical_chain(query.chain) if query.chain else None
        search_filter = canonical_symbol(query.search) if query.search else None
        payload = {"chain": chain_filter} if chain_filter else {}
        data = _ensure_success(await self.transport.post("/api/exchangeRecord/getToken", payload))
        assets: list[Asset] = []
        for item in data.get("tokens", []):
            if isinstance(item, dict):
                asset = Asset(
                    chain=str(item.get("chain", "")),
                    symbol=str(item.get("symbol", "")),
                    name=item.get("name"),
                    address=item.get("address"),
                    decimals=int(item.get("decimals", 0)),
                    logo_url=item.get("logoURI"),
                )
                if chain_filter and canonical_chain(asset.chain) != chain_filter:
                    continue
                searchable = (canonical_symbol(asset.symbol), canonical_symbol(asset.name or ""))
                if search_filter and not any(search_filter in value for value in searchable):
                    continue
                assets.append(asset)
        return assets

    async def quote(self, request: SwapQuoteRequest) -> NormalizedQuote:
        payload = {
            "equipmentNo": _equipment(request.sender_address),
            "sourceFlag": self.source_flag,
            "fromTokenAddress": request.source_asset.address or "",
            "toTokenAddress": request.destination_asset.address or "",
            "fromTokenAmount": request.input_amount_raw,
            "fromTokenChain": request.source_asset.chain,
            "toTokenChain": request.destination_asset.chain,
            "userAddr": request.sender_address,
            "fromCoinCode": f"{request.source_asset.symbol}({request.source_asset.chain})",
            "toCoinCode": f"{request.destination_asset.symbol}({request.destination_asset.chain})",
        }
        data = _ensure_success(await self.transport.post("/api/sswap/quote", payload))
        tx = data.get("txData", {})
        if not isinstance(tx, dict):
            raise ProviderResponseError("100", "Missing txData", category="client")
        reference = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        to_decimals = int(tx.get("toTokenDecimal", request.destination_asset.decimals))
        expected = Decimal(str(tx.get("toTokenAmount", "0")))
        expected_raw = _raw_decimal(expected, to_decimals)
        minimum_raw = str(tx.get("amountOutMin", "0"))
        minimum = Decimal(minimum_raw) / (Decimal(10) ** to_decimals)
        metadata = {
            "request": payload,
            "tx_data": tx,
            "equipment_no": payload["equipmentNo"],
            "source_flag": self.source_flag,
            "from_address": request.sender_address,
            "to_address": request.recipient_address,
            "slippage_bps": request.slippage_bps,
        }
        self._quotes[reference] = metadata
        provider_payload = {
            "equipment_no": payload["equipmentNo"],
            "source_flag": self.source_flag,
            "sender_address": request.sender_address,
            "recipient_address": request.recipient_address,
            "slippage_bps": request.slippage_bps,
            "amount_out_min_raw": minimum_raw,
        }
        spender = self.spender_by_chain.get(request.source_asset.chain.upper()) or self.swap_spender
        if not spender:
            candidate = tx.get("contractAddress") or tx.get("spender") or tx.get("swapContract")
            spender = str(candidate) if candidate else None
        allowance = None
        if (
            spender
            and request.source_asset.address
            and request.source_asset.chain.upper() not in {"ETH", "EVM_NATIVE"}
        ):
            try:
                allowance = AllowanceRequirement(
                    token=request.source_asset,
                    owner=request.sender_address,
                    spender=spender,
                    required_amount_raw=request.input_amount_raw,
                    current_allowance_raw="0",
                )
            except Exception:
                allowance = None
        return NormalizedQuote(
            provider="bridgers",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=expected,
            expected_output_raw=expected_raw,
            minimum_output=minimum,
            minimum_output_raw=minimum_raw,
            provider_fee=Decimal(str(tx["fee"])) if tx.get("fee") is not None else None,
            network_fee=Decimal(str(tx["chainFee"])) if tx.get("chainFee") is not None else None,
            expires_at=None,
            provider_reference=reference,
            provider_payload=provider_payload,
            allowance_requirement=allowance,
        )

    async def prepare(self, quote: NormalizedQuote) -> UnsignedTransaction:
        meta = self._quotes.get(quote.provider_reference) or quote.provider_payload
        request = dict(meta.get("request", {}))
        tx_data = dict(meta.get("tx_data", {}))
        if not request:
            request = {
                "equipmentNo": meta.get("equipment_no", _equipment(meta.get("sender_address", ""))),
                "sourceFlag": meta.get("source_flag", self.source_flag),
                "fromTokenAddress": quote.source_asset.address or "",
                "toTokenAddress": quote.destination_asset.address or "",
                "fromTokenAmount": quote.input_amount_raw,
                "fromTokenChain": quote.source_asset.chain,
                "toTokenChain": quote.destination_asset.chain,
                "userAddr": meta.get("sender_address", ""),
                "fromCoinCode": f"{quote.source_asset.symbol}({quote.source_asset.chain})",
                "toCoinCode": f"{quote.destination_asset.symbol}({quote.destination_asset.chain})",
            }
        request.update(
            {
                "fromAddress": meta.get(
                    "from_address", meta.get("sender_address", request.get("userAddr", ""))
                ),
                "toAddress": meta.get("to_address", meta.get("recipient_address", "")),
                "amountOutMin": quote.minimum_output_raw,
                "slippage": str(Decimal(str(meta.get("slippage_bps", 0))) / Decimal(10000)),
            }
        )
        data = _ensure_success(await self.transport.post("/api/sswap/swap", request))
        tx = data.get("txData", {})
        if not isinstance(tx, dict):
            tx = tx_data
        return UnsignedTransaction(
            chain=quote.source_asset.chain,
            chain_id=quote.source_asset.chain_id,
            to=str(tx.get("to", "")),
            data=str(tx.get("data", "")),
            value=str(tx.get("value", "0x0")),
            gas_limit=None,
            provider="bridgers",
            provider_reference=quote.provider_reference,
            display={
                "input_amount": str(quote.input_amount),
                "expected_output": str(quote.expected_output),
                "minimum_output": str(quote.minimum_output)
                if quote.minimum_output is not None
                else "",
            },
        )

    async def register_broadcast(self, provider_reference: str, tx_hash: str) -> ProviderOrder:
        meta = self._quotes.get(provider_reference, {})
        request = dict(meta.get("request", {}))
        if not request:
            request = {
                "equipmentNo": meta.get("equipment_no", ""),
                "sourceFlag": meta.get("source_flag", self.source_flag),
            }
        request.update(
            {
                "hash": tx_hash,
                "fromAddress": meta.get(
                    "from_address", meta.get("sender_address", request.get("userAddr", ""))
                ),
                "toAddress": meta.get("to_address", meta.get("recipient_address", "")),
                "amountOutMin": (meta.get("tx_data", {}) or {}).get(
                    "amountOutMin", meta.get("amount_out_min_raw", "")
                ),
            }
        )
        data = _ensure_success(
            await self.transport.post(
                "/api/exchangeRecord/updateDataAndStatus",
                request,
                idempotency_key=provider_reference,
            ),
            upload=True,
        )
        order_id = data.get("orderId")
        if not order_id:
            raise ProviderResponseError(
                "100", "Successful upload response is missing orderId", category="client"
            )
        return ProviderOrder(
            provider="bridgers",
            provider_order_id=str(order_id),
            provider_reference=provider_reference,
            tx_hash=tx_hash,
        )

    async def get_status(self, order: ProviderOrder) -> NormalizedOrderStatus:
        data = _ensure_success(
            await self.transport.post(
                "/api/exchangeRecord/getTransDataById",
                {"orderId": order.provider_order_id},
            )
        )
        state = str(data.get("status", "")).strip()
        normalized = self._normalize_status(state)
        details: dict[str, Any] = {"provider_status": state}
        for source, target in (
            ("refundCoinAmt", "refund_amount"),
            ("refundHash", "refund_tx_hash"),
            ("refundHashExplore", "refund_explorer_url"),
            ("refundReason", "refund_reason"),
        ):
            if data.get(source) not in (None, ""):
                details[target] = str(data[source])
        completed_at = self._parse_time(data.get("completeTime") or data.get("updateTime"))
        return NormalizedOrderStatus(
            provider="bridgers",
            provider_order_id=order.provider_order_id,
            status=normalized,
            provider_reference=order.provider_reference,
            tx_hash=data.get("toHash") or data.get("hash"),
            completed_at=completed_at if normalized in {"completed", "refunded"} else None,
            provider_payload=details,
        )

    @staticmethod
    def _normalize_status(state: str) -> str:
        lower = state.lower()
        if lower == "receive_complete":
            return "completed"
        if lower == "refund_complete":
            return "refunded"
        if lower == "error":
            return "processing"
        if "wait" in lower or lower in {"pending", "processing"}:
            return "pending" if "deposit" in lower or lower == "pending" else "processing"
        if lower in {"failed", "fail"}:
            return "failed"
        return "processing"

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace(" ", "T"))
        except ValueError:
            return None
