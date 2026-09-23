"""Normalized OKX transaction explorer adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from wallet_agent.domain.models import (
    TokenTransfer,
    TransactionDetail,
    TransactionHistoryPage,
    TransactionRecord,
    TransactionStatus,
)
from wallet_agent.providers.cache import AsyncTTLCache

from .errors import OkxClientError


class OkxExplorerError(Exception):
    """Stable, secret-safe error at the OKX Explorer boundary."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class OkxExplorerAdapter:
    """Read-only adapter for OKX transaction history and details."""

    _history_path = "/api/v6/dex/post-transaction/transactions-by-address"
    _detail_path = "/api/v6/dex/post-transaction/transaction-detail-by-txhash"
    _chains_path = "/api/v6/dex/explorer/transaction/supported-chains"

    def __init__(
        self,
        client: Any,
        chain_index_by_name: dict[str, str],
        *,
        ttl_seconds: float = 60,
        pending_ttl_seconds: float = 2,
    ) -> None:
        self.client = client
        self.chain_index_by_name = {
            str(name).upper(): str(index) for name, index in chain_index_by_name.items()
        }
        self.chain_name_by_index = {
            index: name for name, index in self.chain_index_by_name.items()
        }
        self._supported_chains: dict[str, set[str]] | None = None
        self._history_cache: AsyncTTLCache[TransactionHistoryPage] = AsyncTTLCache(ttl_seconds)
        self._detail_cache: AsyncTTLCache[TransactionDetail] = AsyncTTLCache(ttl_seconds)
        self._pending_detail_cache: AsyncTTLCache[TransactionDetail] = AsyncTTLCache(
            pending_ttl_seconds
        )
        self._terminal_detail_keys: set[tuple[str, str]] = set()

    async def get_transaction_history(
        self,
        address: str,
        chain: str | None = None,
        *,
        begin_ms: int | None = None,
        end_ms: int | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> TransactionHistoryPage:
        if not address or not isinstance(address, str):
            raise OkxExplorerError("OKX_INVALID_ARGUMENT", "address is required")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise OkxExplorerError("OKX_INVALID_ARGUMENT", "limit must be between 1 and 100")
        query: dict[str, str] = {"address": address, "limit": str(limit)}
        if chain is not None:
            query["chains"] = self._chain_index(chain)
        if begin_ms is not None:
            query["begin"] = self._integer_query("begin_ms", begin_ms)
        if end_ms is not None:
            query["end"] = self._integer_query("end_ms", end_ms)
        if cursor is not None:
            query["cursor"] = str(cursor)
        async def load() -> TransactionHistoryPage:
            payload = await self._request(self._history_path, query=query)
            item = self._first_item(payload, "transaction history")
            rows: Any = None
            rows_present = False
            for key in ("transactionList", "transactions", "list"):
                if key in item:
                    rows = item[key]
                    rows_present = True
                    break
            if not rows_present:
                raise OkxExplorerError(
                    "OKX_MALFORMED_RESPONSE", "OKX transaction history is malformed"
                )
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                raise OkxExplorerError(
                    "OKX_MALFORMED_RESPONSE", "OKX transaction history is malformed"
                )
            transactions = [
                self._record(row, history_kind="full")
                for row in rows
                if isinstance(row, dict)
            ]
            return TransactionHistoryPage(
                transactions=transactions,
                next_cursor=self._optional_string(item.get("cursor", item.get("nextCursor"))),
            )

        return await self._history_cache.get_or_set(
            (address, tuple(sorted(query.items()))), load
        )

    async def get_transaction_detail(self, chain: str, tx_hash: str) -> TransactionDetail:
        if not tx_hash or not isinstance(tx_hash, str):
            raise OkxExplorerError("OKX_INVALID_ARGUMENT", "tx_hash is required")
        chain_index = self._chain_index(chain)

        async def load() -> TransactionDetail:
            payload = await self._request(
                self._detail_path,
                query={"txHash": tx_hash, "chainIndex": chain_index},
            )
            item = self._first_item(payload, "transaction detail")
            record = self._record(item, history_kind="full")
            token_transfers = item.get("tokenTransfers", item.get("tokenTransfer", []))
            if token_transfers is None:
                token_transfers = []
            if not isinstance(token_transfers, list):
                raise OkxExplorerError(
                    "OKX_MALFORMED_RESPONSE", "OKX token transfers are malformed"
                )
            return TransactionDetail(
                **record.model_dump(mode="python"),
                gas_limit=self._optional_string(item.get("gasLimit", item.get("gas_limit"))),
                gas_used=self._optional_string(item.get("gasUsed", item.get("gas_used"))),
                nonce=self._optional_string(item.get("nonce")),
                transaction_index=self._optional_string(
                    item.get("transactionIndex", item.get("txIndex"))
                ),
                block_hash=self._optional_string(item.get("blockHash")),
                fee=self._optional_string(item.get("txFee", item.get("fee"))),
                token_transfers=[
                    TokenTransfer.model_validate(entry)
                    for entry in token_transfers
                    if isinstance(entry, dict)
                ],
            )

        key = (chain_index, tx_hash)
        if key in self._terminal_detail_keys:
            return await self._detail_cache.get_or_set(key, load)
        detail = await self._pending_detail_cache.get_or_set(key, load)
        if detail.status in {TransactionStatus.CONFIRMED, TransactionStatus.FAILED}:
            self._terminal_detail_keys.add(key)

            async def normalized() -> TransactionDetail:
                return detail

            return await self._detail_cache.get_or_set(key, normalized)
        return detail

    async def get_transaction_status(self, chain: str, tx_hash: str) -> TransactionStatus:
        return (await self.get_transaction_detail(chain, tx_hash)).status

    async def get_supported_chains(self) -> dict[str, set[str]]:
        if self._supported_chains is not None:
            return {key: set(value) for key, value in self._supported_chains.items()}
        payload = await self._request(self._chains_path)
        data = payload.get("data")
        if not isinstance(data, list):
            raise OkxExplorerError("OKX_MALFORMED_RESPONSE", "OKX supported chains are malformed")
        result: dict[str, set[str]] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            index = self._optional_string(
                entry.get("chainIndex", entry.get("chainIndexId", entry.get("chainId")))
            )
            name = self._optional_string(entry.get("chainName", entry.get("name")))
            if index and name:
                result.setdefault(name.upper(), set()).add(index)
            elif index:
                result.setdefault(index, set()).add(index)
        if not result:
            raise OkxExplorerError("OKX_MALFORMED_RESPONSE", "OKX supported chains are empty")
        self._supported_chains = result
        return {key: set(value) for key, value in result.items()}

    async def _request(self, path: str, *, query: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            payload = await self.client.request("GET", path, query=query)
        except OkxClientError as exc:
            raise OkxExplorerError(
                "OKX_PROVIDER_ERROR",
                "OKX explorer provider is unavailable.",
                retryable=exc.retryable,
            ) from exc
        except Exception as exc:
            raise OkxExplorerError(
                "OKX_PROVIDER_ERROR", "OKX explorer provider is unavailable."
            ) from exc
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise OkxExplorerError("OKX_MALFORMED_RESPONSE", "OKX explorer response is malformed")
        return payload

    def _chain_index(self, chain: str) -> str:
        value = str(chain)
        index = self.chain_index_by_name.get(value.upper())
        if index is None and value in self.chain_name_by_index:
            index = value
        if index is None:
            raise OkxExplorerError(
                "OKX_CHAIN_UNSUPPORTED",
                "The requested chain is not supported by OKX.",
            )
        return index

    @staticmethod
    def _integer_query(name: str, value: int) -> str:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OkxExplorerError("OKX_INVALID_ARGUMENT", f"{name} must be a non-negative integer")
        return str(value)

    @staticmethod
    def _first_item(payload: dict[str, Any], kind: str) -> dict[str, Any]:
        data = payload.get("data")
        item = data[0] if isinstance(data, list) and data else data
        if not isinstance(item, dict) or not item:
            raise OkxExplorerError("OKX_MALFORMED_RESPONSE", f"OKX {kind} is empty or malformed")
        return item

    def _record(self, item: dict[str, Any], *, history_kind: str) -> TransactionRecord:
        chain_index = self._optional_string(item.get("chainIndex", item.get("chainIndexId")))
        chain = self.chain_name_by_index.get(chain_index or "", chain_index or "UNKNOWN")
        tx_hash = self._optional_string(item.get("txHash", item.get("hash")))
        if not tx_hash:
            raise OkxExplorerError("OKX_MALFORMED_RESPONSE", "OKX transaction hash is missing")
        status = self._status(item.get("txStatus", item.get("status")))
        timestamp = self._timestamp(item.get("txTime", item.get("timestamp")))
        block_number = self._number(item.get("blockHeight", item.get("blockNumber")))
        return TransactionRecord(
            chain=chain,
            chain_id=chain_index,
            tx_hash=tx_hash,
            status=status,
            from_address=item.get("from", item.get("fromAddress")),
            to_address=item.get("to", item.get("toAddress")),
            value=self._optional_string(item.get("amount", item.get("value"))),
            block_number=block_number,
            confirmed_at=timestamp if status is TransactionStatus.CONFIRMED else None,
            source="okx",
            history_kind=history_kind,  # type: ignore[arg-type]
            method_id=item.get("methodId", item.get("methodID")),
        )

    @staticmethod
    def _status(value: Any) -> TransactionStatus:
        text = str(value).strip().lower()
        if text in {"1", "pending", "processing", "wait"}:
            return TransactionStatus.PENDING
        if text in {"2", "success", "succeeded", "confirmed", "completed", "1.0"}:
            return TransactionStatus.CONFIRMED
        if text in {"3", "fail", "failed", "failure", "reverted", "error"}:
            return TransactionStatus.FAILED
        return TransactionStatus.UNKNOWN

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    @staticmethod
    def _number(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(str(value), 0)
        except (TypeError, ValueError):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return None if value in (None, "") else str(value)
