"""Normalized OKX Wallet API adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from wallet_agent.domain.models import (
    AgentError,
    Asset,
    GasLimitEstimate,
    SimulationResult,
    TokenBalance,
    TransactionContext,
    WalletTotalValue,
)

from .errors import OkxClientError
from .models import TokenMarketDetails


class OkxWalletError(Exception):
    """Stable, secret-safe error crossing the OKX wallet boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        super().__init__(message)

    def to_agent_error(self) -> AgentError:
        return AgentError(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            details=self.details,
        )


class OkxWalletAdapter:
    """Map documented OKX wallet and pre-transaction responses to domain models."""

    _native_decimals = {
        "ETH": 18,
        "BSC": 18,
        "BNB": 18,
        "MATIC": 18,
        "POLYGON": 18,
        "AVAX": 18,
        "OKB": 18,
        "TRX": 6,
        "SOL": 9,
    }

    def __init__(self, client: Any, chain_index_by_name: dict[str, str]) -> None:
        self.client = client
        self.chain_index_by_name = {
            str(name).upper(): str(index) for name, index in chain_index_by_name.items()
        }
        self.chain_name_by_index = {
            index: name for name, index in self.chain_index_by_name.items()
        }

    async def get_total_value(
        self,
        address: str,
        chain_indexes: list[str],
        *,
        asset_type: str = "0",
        exclude_risk_tokens: bool = True,
    ) -> WalletTotalValue:
        indexes = self._resolve_chain_indexes(chain_indexes)
        self._validate_asset_type(asset_type)
        payload = await self._request(
            "GET",
            "/api/v6/dex/balance/total-value-by-address",
            query={
                "address": address,
                "chains": ",".join(indexes),
                "assetType": asset_type,
                "excludeRiskToken": exclude_risk_tokens,
            },
        )
        item = self._first_data(payload, "total value")
        total = self._decimal(item.get("totalValue"), "totalValue")
        return WalletTotalValue(
            address=address,
            chain_indexes=indexes,
            asset_type=asset_type,
            exclude_risk_tokens=exclude_risk_tokens,
            total_value=total,
            observed_at=self._observed_at(item),
        )

    async def get_token_balances(
        self,
        address: str,
        chain_indexes: list[str] | None = None,
        *,
        exclude_risk_tokens: bool = True,
    ) -> list[TokenBalance]:
        indexes = self._resolve_chain_indexes(chain_indexes or list(self.chain_name_by_index))
        payload = await self._request(
            "GET",
            "/api/v6/dex/balance/all-token-balances-by-address",
            query={
                "address": address,
                "chains": ",".join(indexes),
                "excludeRiskToken": "0" if exclude_risk_tokens else "1",
            },
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise self._malformed("token balances")
        balances: list[TokenBalance] = []
        for group in data:
            if not isinstance(group, dict):
                raise self._malformed("token balances")
            if any(key in group for key in ("symbol", "tokenSymbol")):
                entries = [group]
            else:
                entries = group.get("tokenAssets", group.get("tokens", group.get("assets", [])))
            if not isinstance(entries, list):
                raise self._malformed("token balances")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise self._malformed("token balance")
                chain_index = str(entry.get("chainIndex") or entry.get("chainIndexId") or "")
                chain = self.chain_name_by_index.get(chain_index)
                if chain is None:
                    raise OkxWalletError(
                        "OKX_CHAIN_UNSUPPORTED",
                        "OKX returned an unsupported chain.",
                        details={"chain_index": chain_index},
                    )
                balances.append(self._token_balance(entry, chain))
        return balances

    async def estimate_gas_limit(self, transaction: TransactionContext) -> GasLimitEstimate:
        payload = await self._pretransaction_request(
            "/api/v6/dex/pre-transaction/gas-limit", transaction
        )
        item = self._first_data(payload, "gas limit")
        gas_limit = item.get("gasLimit", item.get("gas_limit"))
        try:
            result = GasLimitEstimate(
                gas_limit=str(gas_limit),
                chain=transaction.chain,
                observed_at=self._observed_at(item),
            )
        except Exception as exc:
            raise self._malformed("gas limit") from exc
        return result

    async def simulate_transaction(self, transaction: TransactionContext) -> SimulationResult:
        payload = await self._pretransaction_request(
            "/api/v6/dex/pre-transaction/simulate", transaction
        )
        item = self._first_data(payload, "simulation")
        reason = item.get("failReason")
        if reason is None:
            raise self._malformed("simulation")
        return SimulationResult(
            success=not bool(str(reason)),
            gas_used=self._raw_optional(item.get("gasUsed", item.get("gas_used"))),
            failure_reason=str(reason) if reason not in (None, "") else None,
            chain=transaction.chain,
            observed_at=self._observed_at(item),
        )

    async def _pretransaction_request(
        self, path: str, transaction: TransactionContext
    ) -> dict[str, Any]:
        chain_index = self._resolve_chain_indexes([transaction.chain])[0]
        return await self._request(
            "POST",
            path,
            body={
                "chainIndex": chain_index,
                "fromAddress": transaction.from_address,
                "toAddress": transaction.to_address,
                "txAmount": transaction.native_amount,
                "extJson": {"inputData": transaction.calldata},
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            payload = await self.client.request(method, path, **kwargs)
        except OkxClientError as exc:
            raise OkxWalletError(
                "OKX_PROVIDER_ERROR",
                "OKX wallet provider is unavailable.",
                retryable=exc.retryable,
            ) from exc
        except Exception as exc:
            raise OkxWalletError(
                "OKX_PROVIDER_ERROR",
                "OKX wallet provider is unavailable.",
            ) from exc
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise self._malformed("provider response")
        return payload

    def _resolve_chain_indexes(self, chains: list[str]) -> list[str]:
        if not chains:
            raise OkxWalletError("OKX_CHAIN_UNSUPPORTED", "At least one OKX chain is required.")
        resolved: list[str] = []
        for chain in chains:
            value = str(chain)
            index = self.chain_index_by_name.get(value.upper())
            if index is None and value in self.chain_name_by_index:
                index = value
            if index is None:
                raise OkxWalletError(
                    "OKX_CHAIN_UNSUPPORTED",
                    "The requested chain is not supported by OKX.",
                    details={"chain": value},
                )
            resolved.append(index)
        return resolved

    def _token_balance(self, entry: Any, chain: str) -> TokenBalance:
        if not isinstance(entry, dict):
            raise self._malformed("token balance")
        symbol = entry.get("symbol") or entry.get("tokenSymbol")
        if not symbol:
            raise self._malformed("token balance")
        address = entry.get("tokenContractAddress") or entry.get("contractAddress") or None
        is_native = not address
        decimals_value = entry.get("decimals", entry.get("tokenDecimal"))
        if decimals_value is None and not is_native:
            raise self._malformed("token balance")
        if decimals_value is None:
            decimals_value = self._native_decimals.get(str(symbol).upper(), 0) if is_native else 0
        try:
            decimals = int(decimals_value)
        except (TypeError, ValueError) as exc:
            raise self._malformed("token balance") from exc
        asset = Asset(
            chain=chain,
            symbol=str(symbol),
            decimals=decimals,
            address=str(address) if address else None,
        )
        amount = self._decimal(entry.get("balance", entry.get("tokenAmount")), "balance")
        raw_value = entry.get(
            "rawBalance",
            entry.get("balanceRaw", entry.get("tokenAmountRaw")),
        )
        if raw_value in (None, "") and is_native and decimals:
            raw_value = self._derived_raw(amount, decimals)
        raw = self._raw_optional(raw_value)
        if raw is None:
            raise self._malformed("token balance")
        risk = entry.get("isRiskToken", entry.get("riskToken", False))
        if isinstance(risk, str):
            risk = risk.lower() in {"true", "1", "yes"}
        price_value = entry.get(
            "tokenPrice",
            entry.get("tokenUnitPrice", entry.get("price")),
        )
        market = (
            TokenMarketDetails(asset=asset, usd_price=self._decimal(price_value, "tokenPrice"))
            if price_value not in (None, "")
            else None
        )
        usd_value = amount * market.usd_price if market and market.usd_price is not None else None
        return TokenBalance(
            asset=asset,
            amount=amount,
            amount_raw=raw,
            usd_value=usd_value,
            is_risk_token=bool(risk),
            provider="okx",
            observed_at=self._observed_at(entry),
            market_details=market,
        )

    @staticmethod
    def _first_data(payload: dict[str, Any], label: str) -> dict[str, Any]:
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        if isinstance(data, dict):
            return data
        raise OkxWalletError("OKX_MALFORMED_RESPONSE", f"OKX returned malformed {label} data.")

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
            if not parsed.is_finite():
                raise InvalidOperation
            return parsed
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise OkxWalletError(
                "OKX_MALFORMED_RESPONSE", f"OKX returned malformed {field} data."
            ) from exc

    @staticmethod
    def _raw_optional(value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value)
        if not text.isdigit():
            raise OkxWalletError("OKX_MALFORMED_RESPONSE", "OKX returned malformed raw amount.")
        return text

    @staticmethod
    def _derived_raw(amount: Decimal, decimals: int) -> str:
        scaled = amount * (Decimal(10) ** decimals)
        if scaled != scaled.to_integral_value():
            raise OkxWalletError(
                "OKX_MALFORMED_RESPONSE", "OKX returned an imprecise token amount."
            )
        return str(int(scaled))

    @staticmethod
    def _observed_at(item: dict[str, Any]) -> datetime | None:
        value = item.get("observedAt", item.get("timestamp", item.get("time")))
        if value in (None, ""):
            return None
        try:
            number = int(str(value))
            if number > 10_000_000_000:
                number //= 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _validate_asset_type(asset_type: str) -> None:
        if str(asset_type) not in {"0", "1", "2"}:
            raise OkxWalletError("OKX_INVALID_ARGUMENT", "Unsupported OKX asset type.")

    @staticmethod
    def _malformed(label: str) -> OkxWalletError:
        return OkxWalletError(
            "OKX_MALFORMED_RESPONSE",
            f"OKX returned malformed {label} data.",
        )
