"""OmniBridge API adapter."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from wallet_agent.domain.models import (
    Asset,
    AssetQuery,
    DepositOrder,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
)

from .http import ProviderResponseError


def _equipment(address: str) -> str:
    return address[:32]


def _success(response: dict[str, Any]) -> dict[str, Any]:
    code = str(response.get("resCode", ""))
    if code != "800":
        raise ProviderResponseError(
            code, str(response.get("resMsg", "Provider request failed")), category="client"
        )
    data = response.get("data", {})
    return data if isinstance(data, dict) else {}


def _raw(amount: Decimal, decimals: int) -> str:
    return str(int(amount * (Decimal(10) ** decimals)))


class OmniBridgeProvider:
    provider_name = "omnibridge"

    def __init__(self, transport: Any, *, source_flag: str = "", source_type: str = "H5") -> None:
        self.transport = transport
        self.source_flag = source_flag
        self.source_type = source_type
        self._quotes: dict[str, dict[str, Any]] = {}
        self._orders: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_transport(cls, transport: Any, **kwargs: Any) -> "OmniBridgeProvider":
        return cls(transport, **kwargs)

    async def list_assets(self, query: AssetQuery) -> list[Asset]:
        payload = {"sourceFlag": self.source_flag}
        data = _success(await self.transport.post("/api/v1/queryCoinList", payload))
        items = data.get("list", data.get("coins", []))
        return [
            Asset(
                chain=str(x.get("chain", "")),
                symbol=str(x.get("symbol", x.get("coinCode", ""))),
                decimals=int(x.get("decimals", 0)),
                address=x.get("address"),
                name=x.get("name"),
                logo_url=x.get("logoUrl"),
            )
            for x in items
            if isinstance(x, dict)
        ]

    async def quote(self, request: SwapQuoteRequest) -> NormalizedQuote:
        deposit_code = (
            request.source_asset.symbol
            if request.source_asset.chain == "ETH" and not request.source_asset.address
            else f"{request.source_asset.symbol}({request.source_asset.chain})"
        )
        receive_code = f"{request.destination_asset.symbol}({request.destination_asset.chain})"
        payload = {
            "depositCoinCode": deposit_code,
            "receiveCoinCode": receive_code,
            "depositCoinAmt": str(request.input_amount),
            "sourceFlag": self.source_flag,
        }
        data = _success(await self.transport.post("/api/v1/getBaseInfo", payload))
        rate = Decimal(str(data.get("instantRate", "0")))
        fee_rate = Decimal(str(data.get("depositCoinFeeRate", "0")))
        network_fee = Decimal(str(data.get("chainFee", "0")))
        expected = request.input_amount * (Decimal(1) - fee_rate) * rate - network_fee
        if expected < 0:
            expected = Decimal(0)
        reference = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        metadata = {
            "request": payload,
            "quote_data": data,
            "equipment_no": _equipment(request.sender_address),
            "source_type": self.source_type,
            "slippage_bps": request.slippage_bps,
            "destination_addr": request.recipient_address,
            "refund_addr": request.refund_address or request.sender_address,
        }
        self._quotes[reference] = metadata
        return NormalizedQuote(
            provider="omnibridge",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=expected,
            expected_output_raw=_raw(expected, request.destination_asset.decimals),
            provider_fee=request.input_amount * fee_rate,
            network_fee=network_fee,
            provider_reference=reference,
            provider_payload=metadata,
        )

    async def prepare(self, quote: NormalizedQuote) -> DepositOrder:
        meta = self._quotes.get(quote.provider_reference) or quote.provider_payload
        qdata = meta.get("quote_data", {})
        amount = quote.input_amount
        minimum, maximum = (
            Decimal(str(qdata.get("depositMin", "0"))),
            Decimal(str(qdata.get("depositMax", "Infinity"))),
        )
        if amount < minimum or amount > maximum:
            raise ValueError(f"deposit amount must be between {minimum} and {maximum}")
        request = dict(meta.get("request", {}))
        request.update(
            {
                "receiveCoinAmt": str(quote.expected_output),
                "destinationAddr": meta.get("destination_addr", ""),
                "refundAddr": meta.get("refund_addr", ""),
                "equipmentNo": meta.get("equipment_no", ""),
                "sourceType": meta.get("source_type", self.source_type),
                "sourceFlag": self.source_flag,
                "slippage": str(
                    Decimal(str(meta.get("slippage_bps", 200))) / Decimal(10000)
                ),
            }
        )
        data = _success(
            await self.transport.post(
                "/api/v2/accountExchange", request, idempotency_key=quote.provider_reference
            )
        )
        order_id = data.get("orderId")
        if not order_id:
            raise ProviderResponseError(
                "800", "Successful order response is missing orderId", category="client"
            )
        self._orders[str(order_id)] = {**meta, "order_id": str(order_id)}
        return DepositOrder(
            provider="omnibridge",
            provider_order_id=str(order_id),
            deposit_address=str(data.get("platformAddr", "")),
            source_asset=quote.source_asset,
            destination_asset=quote.destination_asset,
            input_amount=quote.input_amount,
            input_amount_raw=quote.input_amount_raw,
            recipient_address=meta.get("destination_addr"),
            refund_address=meta.get("refund_addr"),
            provider_reference=str(order_id),
            provider_payload={
                "equipment_no": meta.get("equipment_no", ""),
                "source_type": meta.get("source_type", self.source_type),
            },
        )

    async def register_broadcast(self, provider_reference: str, tx_hash: str) -> ProviderOrder:
        data = _success(
            await self.transport.post(
                "/api/v2/modifyTxId",
                {"orderId": provider_reference, "depositTxid": tx_hash},
                idempotency_key=provider_reference,
            )
        )
        # Successful modifyTxId responses commonly return a string marker rather than an object.
        if data and not isinstance(data, dict):
            raise ProviderResponseError("800", "Unexpected modifyTxId response", category="client")
        return ProviderOrder(
            provider="omnibridge",
            provider_order_id=provider_reference,
            provider_reference=provider_reference,
            tx_hash=tx_hash,
        )

    async def get_status(self, order: ProviderOrder) -> NormalizedOrderStatus:
        meta = self._orders.get(order.provider_order_id, order.provider_payload)
        payload = {
            "equipmentNo": meta.get("equipment_no", ""),
            "sourceType": meta.get("source_type", self.source_type),
            "orderId": order.provider_order_id,
        }
        data = _success(await self.transport.post("/api/v2/queryOrderState", payload))
        state = str(data.get("detailState", ""))
        mapping = {
            "wait_deposit_send": "pending",
            "wait_exchange_push": "processing",
            "wait_exchange_return": "processing",
            "wait_receive_send": "processing",
            "wait_receive_confirm": "processing",
            "receive_complete": "completed",
            "wait_refund_send": "processing",
            "wait_refund_confirm": "processing",
            "refund_complete": "refunded",
            "timeout": "timed_out",
            "wait_kyc": "kyc_required",
            "error": "processing",
        }
        normalized = mapping.get(state.lower(), "processing")
        details: dict[str, Any] = {"provider_status": state}
        for source, target in (
            ("transactionId", "receive_tx_hash"),
            ("refundCoinAmt", "refund_amount"),
            ("refundDepositTxid", "refund_tx_hash"),
            ("refundReason", "refund_reason"),
            ("kycUrl", "kyc_url"),
        ):
            if data.get(source) not in (None, ""):
                details[target] = str(data[source])
        completed = None
        if data.get("completeTime"):
            try:
                completed = datetime.strptime(str(data["completeTime"]), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        return NormalizedOrderStatus(
            provider="omnibridge",
            provider_order_id=order.provider_order_id,
            status=normalized,
            provider_reference=order.provider_reference,
            tx_hash=data.get("transactionId"),
            refund_address=data.get("refundAddr"),
            completed_at=completed if normalized in {"completed", "refunded"} else None,
            provider_payload=details,
        )
