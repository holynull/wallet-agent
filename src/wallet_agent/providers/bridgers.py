"""Bridgers API adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation
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

from .cache import AsyncTTLCache
from .http import ProviderResponseError

_RETRYABLE = {"412", "413", "415", "777"}
_PERMANENT = {"906", "907", "908", "999", "1114", "1145"}
# Bridgers' getToken filter spells the TRON network as ``TRX`` while catalog
# entries report ``chain: "TRON"``. Passing ``TRON`` returns zero tokens, so a
# TRX(TRON) swap could not resolve its token address (Lif: ETH USDT → TRX
# 报价失败). Map the canonical network label to the API filter spelling.
_CHAIN_FILTER_ALIASES = {"TRON": "TRX"}


def _code(response: dict[str, Any]) -> str:
    return str(response.get("resCode", ""))


def _member(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a dict or an object."""
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _coin_code(asset: Any) -> str:
    """Render a Bridgers ``COIN(CHAIN)`` code from an Asset (dict or object)."""
    if asset is None:
        return ""
    return f"{_member(asset, 'symbol', '')}({_member(asset, 'chain', '')})"


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


def _raw_decimal_up(value: Decimal, decimals: int) -> str:
    scale = Decimal(10) ** decimals
    return str(int((value * scale).to_integral_value(rounding=ROUND_CEILING)))


def _output_meets_target(actual: Decimal, target: Decimal, decimals: int) -> bool:
    """Compare exact-output quotes after truncating to destination raw units."""
    scale = Decimal(10) ** decimals
    actual_raw = int((actual * scale).to_integral_value(rounding=ROUND_DOWN))
    target_raw = int((target * scale).to_integral_value(rounding=ROUND_DOWN))
    return actual_raw >= target_raw


def _quote_decimal(value: Any, field: str, *, nonnegative: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProviderResponseError("100", f"Invalid {field}", category="client") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ProviderResponseError("100", f"Invalid {field}", category="client")
    return result


def _catalog_symbol_matches(asset: Asset, candidate: Asset) -> bool:
    target = canonical_symbol(asset.symbol)
    symbol = canonical_symbol(candidate.symbol)
    if symbol == target:
        return True
    chain_suffix = f"({canonical_chain(asset.chain)})"
    return symbol.removesuffix(chain_suffix) == target


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
        asset_cache_ttl_seconds: float = 600,
    ) -> None:
        self.transport = transport
        self.source_flag = source_flag
        self.source_type = source_type
        self.spender_by_chain = {
            str(k).upper(): str(v) for k, v in (spender_by_chain or {}).items()
        }
        self.swap_spender = swap_spender
        self._quotes: dict[str, dict[str, Any]] = {}
        self._asset_cache: AsyncTTLCache[list[Asset]] = AsyncTTLCache(asset_cache_ttl_seconds)

    @classmethod
    def from_transport(cls, transport: Any, **kwargs: Any) -> "BridgersProvider":
        return cls(transport, **kwargs)

    async def list_assets(self, query: AssetQuery) -> list[Asset]:
        chain_filter = canonical_chain(query.chain) if query.chain else None
        search_filter = canonical_symbol(query.search) if query.search else None
        # The API filter spelling can differ from the chain label the catalog
        # reports (TRON → ``TRX``), so keep the two apart: ``api_chain`` is
        # sent to Bridgers, ``chain_filter`` still matches returned entries.
        api_chain = (
            _CHAIN_FILTER_ALIASES.get(chain_filter, chain_filter) if chain_filter else None
        )
        payload = {"chain": api_chain} if api_chain else {}

        async def load() -> list[Asset]:
            data = _ensure_success(
                await self.transport.post("/api/exchangeRecord/getToken", payload)
            )
            assets: list[Asset] = []
            tokens = data.get("tokens") or []
            if not isinstance(tokens, list):
                raise ProviderResponseError("100", "Malformed token catalog", category="client")
            for item in tokens:
                if not isinstance(item, dict):
                    continue
                try:
                    decimals = int(item.get("decimals", 0))
                except (TypeError, ValueError) as exc:
                    raise ProviderResponseError(
                        "100", "Invalid catalog decimals", category="client"
                    ) from exc
                if not 0 <= decimals <= 255:
                    raise ProviderResponseError(
                        "100", "Invalid catalog decimals", category="client"
                    )
                asset = Asset(
                    chain=str(item.get("chain", "")),
                    symbol=str(item.get("symbol", "")),
                    name=item.get("name"),
                    address=item.get("address"),
                    decimals=decimals,
                    logo_url=item.get("logoURI"),
                )
                if chain_filter and canonical_chain(asset.chain) != chain_filter:
                    continue
                assets.append(asset)
            return assets

        assets = await self._asset_cache.get_or_set(chain_filter or "*", load)
        if search_filter:
            return [
                asset
                for asset in assets
                if any(
                    search_filter in value
                    for value in (
                        canonical_symbol(asset.symbol),
                        canonical_symbol(asset.name or ""),
                    )
                )
            ]
        return list(assets)

    async def _resolve_token_address(self, asset: Asset) -> str:
        """Resolve an address required by Bridgers for a native asset.

        The graph intentionally models native assets without a contract address.
        Bridgers' quote API, however, requires the provider catalog address for
        both sides of a quote.  Resolve only missing addresses here so explicit
        token addresses remain untouched.
        """
        if asset.address:
            return asset.address
        matches = await self.list_assets(AssetQuery(chain=asset.chain, search=asset.symbol))
        addresses = {
            str(candidate.address)
            for candidate in matches
            if candidate.address
            and canonical_chain(candidate.chain) == canonical_chain(asset.chain)
            and _catalog_symbol_matches(asset, candidate)
        }
        if len(addresses) != 1:
            raise ProviderResponseError(
                "100",
                f"Bridgers token catalog has no unique address for {asset.symbol} on {asset.chain}",
                category="client",
            )
        return next(iter(addresses))

    async def quote(self, request: SwapQuoteRequest) -> NormalizedQuote:
        source_address = await self._resolve_token_address(request.source_asset)
        destination_address = await self._resolve_token_address(request.destination_asset)
        payload = {
            "equipmentNo": _equipment(request.sender_address),
            "sourceFlag": self.source_flag,
            "fromTokenAddress": source_address,
            "toTokenAddress": destination_address,
            "fromTokenAmount": request.input_amount_raw,
            "fromTokenChain": request.source_asset.chain,
            "toTokenChain": request.destination_asset.chain,
            "userAddr": request.sender_address,
            "fromCoinCode": f"{request.source_asset.symbol}({request.source_asset.chain})",
            "toCoinCode": f"{request.destination_asset.symbol}({request.destination_asset.chain})",
        }
        data = _ensure_success(await self.transport.post("/api/sswap/quote", payload))
        tx = data.get("txData")
        if not isinstance(tx, dict) or not tx:
            raise ProviderResponseError("100", "Missing txData", category="client")
        self.minimum_source_amount = str(tx.get("depositMin", "0"))
        reference = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        try:
            to_decimals = int(tx.get("toTokenDecimal", request.destination_asset.decimals))
        except (TypeError, ValueError) as exc:
            raise ProviderResponseError("100", "Invalid toTokenDecimal", category="client") from exc
        if not 0 <= to_decimals <= 255:
            raise ProviderResponseError("100", "Invalid toTokenDecimal", category="client")
        expected = _quote_decimal(tx.get("toTokenAmount", "0"), "toTokenAmount")
        expected_raw = _raw_decimal(expected, to_decimals)
        minimum_raw = str(tx.get("amountOutMin", "0"))
        minimum = _quote_decimal(minimum_raw, "amountOutMin") / (Decimal(10) ** to_decimals)
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
            "from_token_address": source_address,
            "to_token_address": destination_address,
            "slippage_bps": request.slippage_bps,
            "amount_out_min_raw": minimum_raw,
        }
        spender = self.spender_by_chain.get(request.source_asset.chain.upper()) or self.swap_spender
        if not spender:
            candidate = tx.get("contractAddress") or tx.get("spender") or tx.get("swapContract")
            spender = str(candidate) if candidate else None
        allowance = None
        if spender and request.source_asset.address:
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
            provider_fee=_quote_decimal(tx["fee"], "fee") if tx.get("fee") is not None else None,
            network_fee=_quote_decimal(tx["chainFee"], "chainFee")
            if tx.get("chainFee") is not None
            else None,
            expires_at=None,
            provider_reference=reference,
            provider_payload=provider_payload,
            allowance_requirement=allowance,
            slippage_bps=request.slippage_bps,
        )

    async def reverse_quote(
        self, request: SwapQuoteRequest, output_amount: Decimal
    ) -> NormalizedQuote:
        """Estimate a bounded source amount and verify it with forward quotes."""
        if output_amount <= 0:
            raise ValueError("target output amount must be greater than zero")
        decimals = request.source_asset.decimals
        quantum = Decimal(1) / (Decimal(10) ** decimals)
        probe_amount = max(quantum, Decimal("1"))
        probe = request.model_copy(
            update={
                "input_amount": probe_amount,
                "input_amount_raw": _raw_decimal(probe_amount, decimals),
            }
        )
        probe_quote = await self.quote(probe)
        meta = self._quotes.get(probe_quote.provider_reference, {})
        tx = meta.get("tx_data", {}) if isinstance(meta, dict) else {}
        minimum = Decimal(str(tx.get("depositMin", "0")))
        maximum = Decimal(str(tx.get("depositMax", "0")))
        if maximum <= 0:
            maximum = probe_amount * Decimal("1000000")
        lower = max(minimum, quantum)
        upper = maximum
        if lower > upper:
            raise ValueError(f"Bridgers source bounds are invalid: {lower}..{upper}")
        if probe_quote.expected_output <= 0:
            raise ValueError("Bridgers probe quote returned no output")

        # Provider rates can move between calls. A proportional estimate plus a
        # small upward correction is both faster and more stable than binary
        # searching every 18-decimal raw unit (which can require ~70 HTTP calls).
        candidate = probe_amount * output_amount / probe_quote.expected_output
        candidate = max(lower, candidate)
        safety_factor = Decimal("1.001")
        last_quote = probe_quote
        previous_raw: int | None = None
        for _attempt in range(4):
            raw = int(_raw_decimal_up(candidate, decimals))
            if previous_raw is not None and raw <= previous_raw:
                raw = previous_raw + 1
            amount = Decimal(raw) / (Decimal(10) ** decimals)
            if amount > upper:
                raise ValueError(
                    f"Bridgers cannot reach requested output {output_amount} within source bounds"
                )
            if raw == int(probe.input_amount_raw):
                quote = probe_quote
            else:
                quote = await self.quote(
                    request.model_copy(
                        update={"input_amount": amount, "input_amount_raw": str(raw)}
                    )
                )
            last_quote = quote
            if _output_meets_target(
                quote.expected_output, output_amount, request.destination_asset.decimals
            ):
                return quote
            previous_raw = raw
            if quote.expected_output <= 0:
                candidate = amount * Decimal("2")
            else:
                candidate = amount * output_amount / quote.expected_output * safety_factor
            candidate = max(candidate, amount + quantum, lower)

        raise ValueError(
            "Bridgers reverse quote verification fell below target output "
            f"after bounded retries (last output: {last_quote.expected_output})"
        )

    async def prepare(self, quote: NormalizedQuote) -> UnsignedTransaction:
        meta = self._quotes.get(quote.provider_reference) or quote.provider_payload
        request = dict(meta.get("request", {}))
        tx_data = dict(meta.get("tx_data", {}))
        if not request:
            request = {
                "equipmentNo": meta.get("equipment_no", _equipment(meta.get("sender_address", ""))),
                "sourceFlag": meta.get("source_flag", self.source_flag),
                "fromTokenAddress": meta.get(
                    "from_token_address", quote.source_asset.address or ""
                ),
                "toTokenAddress": meta.get(
                    "to_token_address", quote.destination_asset.address or ""
                ),
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
                "slippage": str(Decimal(str(meta.get("slippage_bps", 100))) / Decimal(10000)),
            }
        )
        data = _ensure_success(await self.transport.post("/api/sswap/swap", request))
        tx = data.get("txData", {})
        if not isinstance(tx, dict):
            tx = tx_data
        # UTXO chains get a deposit address instead of calldata, and Bridgers
        # names it ``toAddress`` there (EVM payloads use ``to``). An empty
        # destination would otherwise surface as an unsigned transfer to nowhere.
        destination = str(tx.get("to") or tx.get("toAddress") or "")
        if not destination:
            raise ProviderResponseError(
                "100", "Provider returned no deposit address", category="client"
            )
        display = {
            "input_amount": str(quote.input_amount),
            "expected_output": str(quote.expected_output),
            "minimum_output": str(quote.minimum_output)
            if quote.minimum_output is not None
            else "",
        }
        memo = str(tx.get("memo") or "")
        if memo:
            # Bitcoin deposits carry the routing hint the provider uses for the
            # order; show it rather than dropping it.
            display["memo"] = memo
        return UnsignedTransaction(
            chain=quote.source_asset.chain,
            chain_id=quote.source_asset.chain_id,
            to=destination,
            data=str(tx.get("data", "")),
            value=str(tx.get("value", "0x0")),
            gas_limit=None,
            provider="bridgers",
            provider_reference=quote.provider_reference,
            display=display,
        )

    async def register_broadcast(
        self,
        provider_reference: str,
        tx_hash: str,
        *,
        quote: Any | None = None,
    ) -> ProviderOrder:
        meta = self._quotes.get(provider_reference, {})
        payload = dict(_member(quote, "provider_payload", {}) or {})
        request = dict(meta.get("request", {}))
        if not request:
            # The in-memory quote cache can miss (process restart, a different
            # provider instance, a long-lived session). Bridgers rejects the
            # minimal fallback with resCode 101 "required parameter is empty",
            # which surfaced to App as 503 PROVIDER_REGISTRATION_FAILED. Rebuild
            # the full request from the persisted session quote instead. The
            # quote may arrive as a NormalizedQuote (API path) or its JSON dict
            # (graph state path), so read fields through _member.
            source_asset = _member(quote, "source_asset", None)
            destination_asset = _member(quote, "destination_asset", None)
            request = {
                "equipmentNo": str(payload.get("equipment_no", "") or ""),
                "sourceFlag": str(payload.get("source_flag") or self.source_flag),
                "fromTokenAddress": str(payload.get("from_token_address", "") or ""),
                "toTokenAddress": str(payload.get("to_token_address", "") or ""),
                "fromTokenAmount": str(
                    _member(quote, "input_amount_raw", "") or ""
                ),
                "fromTokenChain": str(_member(source_asset, "chain", "") or ""),
                "toTokenChain": str(_member(destination_asset, "chain", "") or ""),
                "userAddr": str(payload.get("sender_address", "") or ""),
                "fromCoinCode": _coin_code(source_asset),
                "toCoinCode": _coin_code(destination_asset),
            }
        request.update(
            {
                "hash": tx_hash,
                "fromAddress": meta.get(
                    "from_address",
                    meta.get("sender_address", request.get("userAddr", "")),
                )
                or str(payload.get("sender_address", "") or ""),
                "toAddress": meta.get(
                    "to_address", meta.get("recipient_address", "")
                )
                or str(payload.get("recipient_address", "") or ""),
                "amountOutMin": (
                    (meta.get("tx_data", {}) or {}).get("amountOutMin")
                    or meta.get("amount_out_min_raw", "")
                    or str(payload.get("amount_out_min_raw", "") or "")
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
            failure_reason=next(
                (
                    str(data[key])
                    for key in ("refundReason", "errMsg", "errorMsg")
                    if data.get(key) not in (None, "")
                ),
                None,
            ),
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
