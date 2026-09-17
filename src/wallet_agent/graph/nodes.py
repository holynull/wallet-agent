"""Small graph nodes and runtime adapters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from wallet_agent.chains._common import receipt_success
from wallet_agent.domain.models import (
    AgentError,
    ApprovalTransaction,
    Asset,
    AssetQuery,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    TransferRequest,
    UnsignedTransaction,
)


@dataclass(frozen=True)
class GraphRuntime:
    model: Any
    providers: dict[str, Any]
    chains: dict[str, Any]
    price_provider: Any | None = None
    max_poll_attempts: int = 3
    confirmation_ttl_seconds: int = 900


_VALID_INTENTS = {
    "wallet_query",
    "swap_quote",
    "swap_prepare",
    "swap_status",
    "clarification",
    "unsupported",
    "transfer",
    "swap_select",
    "swap_allowance",
    "price_query",
    "transaction_status",
    "portfolio_query",
    "gas_check",
    "asset_discovery",
}


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _fee_dump(value: Any) -> dict[str, Any]:
    dumped = _dump(value)
    if isinstance(dumped, dict):
        return dumped
    result = {
        key: getattr(value, key)
        for key in ("chain", "chain_id", "amount_raw", "gas_limit", "expires_at")
        if hasattr(value, key)
    }
    if hasattr(value, "amount"):
        result["amount"] = str(value.amount)
    if hasattr(value, "asset"):
        asset = _dump(value.asset)
        result["asset"] = asset if isinstance(asset, dict) else str(asset)
    return result


def _quote(value: Any) -> NormalizedQuote:
    return value if isinstance(value, NormalizedQuote) else NormalizedQuote.model_validate(value)


def _request(value: Any) -> SwapQuoteRequest:
    return SwapQuoteRequest.model_validate(_dump(value))


def _request_json(value: Any) -> dict[str, Any]:
    """Return the canonical JSON-safe representation used by graph/tool state."""
    return _request(value).model_dump(mode="json")


def _mapping(value: Any) -> dict[str, Any]:
    """Coerce legacy structured input to a plain JSON-safe mapping."""
    dumped = _dump(value)
    return dumped if isinstance(dumped, dict) else {}


def _error(
    code: str, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return AgentError(
        code=code, message=message, retryable=retryable, details=details or {}
    ).model_dump(mode="json")


def _parse_model_output(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        result = value.model_dump()
        return result if isinstance(result, dict) else None
    if isinstance(value, str):
        try:
            result = json.loads(value)
        except (TypeError, ValueError):
            return None
        return result if isinstance(result, dict) else None
    return None


def _confirmation_snapshot(
    *,
    action: str,
    status: str,
    summary: dict[str, Any],
    ttl_seconds: int,
    reason: str | None = None,
    requested_at: datetime | None = None,
) -> dict[str, Any]:
    now = requested_at or datetime.now(timezone.utc)
    return {
        "action": action,
        "status": status,
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        "summary": summary,
        "reason": reason,
    }


def _confirmation_expired(value: dict[str, Any]) -> bool:
    raw = value.get("expires_at")
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(str(raw))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except ValueError:
        return True


def _looks_like_swap_message(message: str) -> bool:
    text = message.lower()
    return any(token in text for token in ("swap", "exchange", "兑换", "换成", "换"))


def _looks_like_cancel_message(message: str) -> bool:
    text = "".join(str(message).lower().split())
    return any(
        token in text
        for token in ("取消兑换", "取消交易", "停止兑换", "停止交易", "cancelswap", "cancel")
    )


def _task_state(
    previous: dict[str, Any] | None,
    *,
    goal: str,
    stage: str,
    slots: dict[str, Any] | None = None,
    missing_fields: list[str] | None = None,
    status: str = "active",
    updated_by: str = "intent",
) -> dict[str, Any]:
    """Build the checkpoint-safe conversation contract shared by turns."""
    old = previous or {}
    return {
        "goal": goal,
        "stage": stage,
        "status": status,
        "slots": dict(slots if slots is not None else old.get("slots") or {}),
        "missing_fields": list(missing_fields or []),
        "updated_by": updated_by,
    }


def _task_update(
    previous: dict[str, Any] | None,
    *,
    goal: str,
    stage: str,
    slots: dict[str, Any] | None = None,
    missing_fields: list[str] | None = None,
    status: str = "active",
) -> dict[str, Any]:
    snapshot = _task_state(
        previous,
        goal=goal,
        stage=stage,
        slots=slots,
        missing_fields=missing_fields,
        status=status,
    )
    return {"conversation_state": snapshot, "task_stage": stage}


def _merge_swap_slots(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge user corrections and invalidate metadata derived from changed slots."""
    merged = dict(existing)
    changed = {
        key: value
        for key, value in incoming.items()
        if value is not None and value != merged.get(key)
    }
    merged.update(changed)
    if "source_chain" in changed or "source_symbol" in changed:
        if "source_token_address" not in changed:
            merged.pop("source_token_address", None)
        if "source_decimals" not in changed:
            merged.pop("source_decimals", None)
        for field in ("source_chain_id", "source_name", "source_logo_url"):
            if field not in changed:
                merged.pop(field, None)
    if "destination_chain" in changed or "destination_symbol" in changed:
        if "destination_token_address" not in changed:
            merged.pop("destination_token_address", None)
        if "destination_decimals" not in changed:
            merged.pop("destination_decimals", None)
        for field in ("destination_chain_id", "destination_name", "destination_logo_url"):
            if field not in changed:
                merged.pop(field, None)
    if "input_amount" in changed and "input_amount_raw" not in changed:
        merged.pop("input_amount_raw", None)
    return merged


def _swap_draft_from_request(request: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten a request so an older checkpoint can re-enter slot filling."""
    raw = _dump(request) or {}
    if not isinstance(raw, dict):
        return {}
    draft: dict[str, Any] = {
        key: raw.get(key)
        for key in ("input_amount", "input_amount_raw", "sender_address", "recipient_address")
        if raw.get(key) is not None
    }
    for side, asset_key in (("source", "source_asset"), ("destination", "destination_asset")):
        asset = _dump(raw.get(asset_key)) or {}
        if not isinstance(asset, dict):
            asset = {}
        for field in ("chain", "chain_id", "symbol", "address", "decimals", "name", "logo_url"):
            value = asset.get(field)
            if value is not None:
                draft[f"{side}_{'token_address' if field == 'address' else field}"] = value
    for field in ("refund_address", "slippage_bps", "expires_at"):
        if raw.get(field) is not None:
            draft[field] = raw[field]
    return draft


def _clarification_message(
    *,
    message: str | None = None,
    missing: list[str] | None = None,
    swap: bool = False,
) -> str:
    labels = {
        "source_chain": "来源链（例如 Ethereum、Base、BSC）",
        "destination_chain": "目标链（例如 Ethereum、Base、BSC）",
        "source_symbol": "要换出的 Token",
        "destination_symbol": "要换入的 Token",
        "source_token_address": "来源 Token 合约地址",
        "destination_token_address": "目标 Token 合约地址",
        "source_decimals": "来源 Token 精度",
        "destination_decimals": "目标 Token 精度",
        "input_amount": "兑换数量",
        "sender_address": "钱包地址",
        "recipient_address": "接收地址",
    }
    if swap and missing:
        readable = [labels.get(item, item) for item in missing]
        return (
            "可以帮你兑换。"
            f"还需要确认：{'、'.join(readable)}。"
            "请告诉我来源 Token 和链、目标 Token 和链；如果已连接钱包，钱包地址会自动使用。"
        )
    if message:
        return message
    return (
        "你好！我可以帮你查询余额、比较兑换报价、发起转账或兑换。请告诉我具体的 Token、链和数量。"
    )


_SWAP_DRAFT_KEYS = (
    "source_chain",
    "source_chain_id",
    "destination_chain",
    "destination_chain_id",
    "source_symbol",
    "destination_symbol",
    "source_token_address",
    "destination_token_address",
    "source_decimals",
    "destination_decimals",
    "source_name",
    "destination_name",
    "source_logo_url",
    "destination_logo_url",
    "input_amount",
    "input_amount_raw",
    "sender_address",
    "recipient_address",
    "refund_address",
    "slippage_bps",
    "expires_at",
)

_TRANSFER_DRAFT_KEYS = (
    "transfer_chain",
    "transfer_symbol",
    "transfer_token_address",
    "transfer_decimals",
    "transfer_amount",
    "transfer_amount_raw",
    "transfer_sender",
    "transfer_recipient",
)

_TRANSACTION_QUERY_KEYS = ("transaction_chain", "transaction_hash")
_PORTFOLIO_QUERY_KEYS = ("portfolio_chain",)
_GAS_QUERY_KEYS = ("gas_chain", "gas_to", "gas_data")
_ASSET_QUERY_KEYS = ("asset_chain", "asset_search")


def _native_symbol(chain: str, wallet_context: dict[str, Any] | None) -> str | None:
    context_symbol = (wallet_context or {}).get("native_symbol")
    if context_symbol:
        return str(context_symbol).upper()
    return {
        "EVM": "ETH",
        "ETH": "ETH",
        "BASE": "ETH",
        "ARBITRUM": "ETH",
        "ARB": "ETH",
        "OPTIMISM": "ETH",
        "OP": "ETH",
        "BSC": "BNB",
        "POLYGON": "MATIC",
        "TRON": "TRX",
        "TRX": "TRX",
        "SOLANA": "SOL",
        "SOL": "SOL",
    }.get(chain.upper())


def _transfer_draft_request(
    draft: dict[str, Any], wallet_context: dict[str, Any] | None
) -> tuple[TransferRequest | None, list[str]]:
    context = wallet_context or {}
    normalized = dict(draft)
    normalized.setdefault("chain", normalized.get("transfer_chain") or context.get("chain"))
    normalized.setdefault("sender", normalized.get("transfer_sender") or context.get("address"))
    normalized.setdefault("recipient", normalized.get("transfer_recipient"))
    normalized.setdefault("amount", normalized.get("transfer_amount"))
    normalized.setdefault("amount_raw", normalized.get("transfer_amount_raw"))
    normalized.setdefault("symbol", normalized.get("transfer_symbol"))
    normalized.setdefault("token_address", normalized.get("transfer_token_address"))
    normalized.setdefault("decimals", normalized.get("transfer_decimals"))
    if not normalized.get("sender"):
        normalized["sender"] = normalized.get("transfer_sender")
    if normalized.get("chain") and normalized.get("transfer_chain") is None:
        normalized["transfer_chain"] = normalized["chain"]

    missing = [
        field
        for field in ("chain", "sender", "recipient", "amount")
        if normalized.get(field) in (None, "")
    ]
    if missing:
        return None, [f"transfer_{field}" for field in missing]
    symbol = normalized.get("symbol")
    token_address = normalized.get("token_address")
    decimals = normalized.get("decimals")
    native_symbol = _native_symbol(str(normalized["chain"]), context)
    is_native = not token_address and (
        not symbol or (native_symbol and str(symbol).upper() == native_symbol)
    )
    if not is_native:
        missing_token = []
        if not token_address:
            missing_token.append("transfer_token_address")
        if decimals is None:
            missing_token.append("transfer_decimals")
        if missing_token:
            return None, missing_token
    if not normalized.get("amount_raw"):
        try:
            amount = Decimal(str(normalized["amount"]))
            amount_decimals = 18 if is_native else int(decimals)
            raw = amount * (Decimal(10) ** amount_decimals)
            if raw != raw.to_integral_value():
                raise ValueError("transfer amount has more precision than token decimals")
            normalized["amount_raw"] = str(int(raw))
        except Exception:
            return None, ["transfer_amount_raw"]
    token = None
    if not is_native:
        token = {
            "chain": normalized["chain"],
            "symbol": str(symbol or "TOKEN"),
            "decimals": int(decimals),
            "address": token_address,
        }
    request = {
        "chain": normalized["chain"],
        "sender": normalized["sender"],
        "recipient": normalized["recipient"],
        "amount": str(normalized["amount"]),
        "amount_raw": str(normalized["amount_raw"]),
        "token": token,
    }
    try:
        return TransferRequest.model_validate(request), []
    except Exception:
        return None, ["transfer_request"]


async def _transaction_preflight(
    *,
    adapter: Any,
    chain: str,
    sender: str,
    recipient: str,
    amount_raw: str,
    token: Any,
    transaction: Any,
    wallet_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run read-only checks and keep unavailable optional checks non-blocking."""
    checks: list[dict[str, Any]] = []
    context = wallet_context or {}

    def add(
        name: str,
        status: str,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        item: dict[str, Any] = {"name": name, "status": status, "message": message}
        if code:
            item["code"] = code
        if details:
            item["details"] = details
        checks.append(item)

    if not sender:
        add(
            "wallet_account",
            "warning",
            "缺少发送方上下文，无法核对当前钱包账户。",
            code="CHECK_UNAVAILABLE",
        )
    elif context.get("address") and str(context["address"]).lower() != sender.lower():
        add(
            "wallet_account",
            "failed",
            "当前连接的钱包账户与交易发送方不一致。",
            code="WALLET_ACCOUNT_MISMATCH",
            details={"wallet_address": context["address"], "sender": sender},
        )
    else:
        add("wallet_account", "passed", "发送方与当前钱包账户一致。")

    if context.get("chain") and str(context["chain"]).upper() != chain.upper():
        add(
            "wallet_chain",
            "warning",
            f"当前钱包在 {context['chain']}，签名时需要切换到 {chain}。",
            code="WALLET_CHAIN_SWITCH_REQUIRED",
            details={"wallet_chain": context["chain"], "required_chain": chain},
        )
    else:
        add("wallet_chain", "passed", f"钱包网络为 {chain}。")

    if hasattr(adapter, "validate_address") and sender:
        try:
            sender_valid = await adapter.validate_address(sender)
            recipient_valid = await adapter.validate_address(recipient) if recipient else True
            if not sender_valid or not recipient_valid:
                add(
                    "addresses",
                    "failed",
                    "发送方或收款方地址格式无效。",
                    code="INVALID_ADDRESS",
                    details={"sender_valid": sender_valid, "recipient_valid": recipient_valid},
                )
            elif recipient:
                add("addresses", "passed", "发送方和收款方地址格式有效。")
            else:
                add(
                    "addresses",
                    "warning",
                    "缺少收款方上下文，仅完成发送方地址校验。",
                    code="CHECK_PARTIAL",
                )
        except Exception as exc:
            add("addresses", "failed", str(exc), code="ADDRESS_VALIDATION_FAILED")
    elif not sender:
        add("addresses", "warning", "缺少地址上下文，无法完成地址校验。", code="CHECK_UNAVAILABLE")
    else:
        add("addresses", "warning", "当前链适配器不支持地址校验。", code="CHECK_UNAVAILABLE")

    if token is not None and str(token.chain).upper() != chain.upper():
        add("token_chain", "failed", "Token 所属链与交易链不一致.", code="TOKEN_CHAIN_MISMATCH")

    fee = None
    if hasattr(adapter, "estimate_fee"):
        try:
            fee = await adapter.estimate_fee(to=transaction.to, data=transaction.data)
            add("network_fee", "passed", "已获取网络手续费估算。")
        except Exception as exc:
            add(
                "network_fee",
                "warning",
                "暂时无法估算网络手续费，签名前请在钱包中确认。",
                code="FEE_ESTIMATE_UNAVAILABLE",
                details={"error": str(exc)},
            )
    else:
        add("network_fee", "warning", "当前链适配器不支持手续费估算。", code="CHECK_UNAVAILABLE")

    native_balance = None
    if hasattr(adapter, "get_native_balance"):
        try:
            native_balance = await adapter.get_native_balance(sender)
        except Exception as exc:
            add(
                "native_balance",
                "warning",
                "暂时无法读取原生币余额。",
                code="BALANCE_CHECK_UNAVAILABLE",
                details={"error": str(exc)},
            )

    if token is None:
        if native_balance is not None:
            fee_raw = int(fee.amount_raw) if fee is not None else 0
            required = int(amount_raw) + fee_raw
            if int(native_balance.amount_raw) < required:
                add(
                    "balance",
                    "failed",
                    "原生币余额不足以支付转账金额和网络手续费。",
                    code="INSUFFICIENT_BALANCE",
                    details={
                        "balance_raw": native_balance.amount_raw,
                        "required_raw": str(required),
                        "fee_raw": fee.amount_raw if fee is not None else None,
                    },
                )
            else:
                add("balance", "passed", "原生币余额足够覆盖金额和手续费。")
    else:
        if not hasattr(adapter, "get_token_balance"):
            add(
                "token_balance",
                "warning",
                "当前链适配器不支持 Token 余额检查。",
                code="CHECK_UNAVAILABLE",
            )
        else:
            try:
                balance = await adapter.get_token_balance(token, sender)
                if int(balance.amount_raw) < int(amount_raw):
                    add(
                        "token_balance",
                        "failed",
                        f"{token.symbol} 余额不足。",
                        code="INSUFFICIENT_BALANCE",
                        details={"balance_raw": balance.amount_raw, "required_raw": amount_raw},
                    )
                else:
                    add("token_balance", "passed", f"{token.symbol} 余额足够。")
            except Exception as exc:
                add(
                    "token_balance",
                    "failed",
                    "暂时无法读取 Token 余额。",
                    code="BALANCE_CHECK_FAILED",
                    details={"error": str(exc)},
                )
        if native_balance is not None and fee is not None:
            if int(native_balance.amount_raw) < int(fee.amount_raw):
                add(
                    "gas_balance",
                    "failed",
                    "原生币余额不足以支付网络手续费。",
                    code="INSUFFICIENT_GAS",
                    details={"balance_raw": native_balance.amount_raw, "fee_raw": fee.amount_raw},
                )
            else:
                add("gas_balance", "passed", "原生币余额足够支付网络手续费。")

    failed = [item for item in checks if item["status"] == "failed"]
    return {
        "ok": not failed,
        "available": True,
        "chain": chain,
        "sender": sender,
        "recipient": recipient,
        "fee_estimate": _fee_dump(fee) if fee is not None else None,
        "checks": checks,
        "warnings": [item for item in checks if item["status"] == "warning"],
    }


def _swap_draft_request(
    draft: dict[str, Any], wallet_context: dict[str, Any] | None
) -> tuple[SwapQuoteRequest | None, list[str]]:
    context = wallet_context or {}
    normalized = dict(draft)
    normalized.setdefault("sender_address", context.get("address"))
    normalized.setdefault("recipient_address", context.get("address"))
    required = (
        "source_chain",
        "destination_chain",
        "source_symbol",
        "destination_symbol",
        "source_token_address",
        "destination_token_address",
        "source_decimals",
        "destination_decimals",
        "input_amount",
        "sender_address",
        "recipient_address",
    )
    missing = [field for field in required if normalized.get(field) in (None, "")]
    if missing:
        return None, missing
    if not normalized.get("input_amount_raw"):
        try:
            amount = Decimal(str(normalized["input_amount"]))
            decimals = int(normalized["source_decimals"])
            raw = amount * (Decimal(10) ** decimals)
            if raw != raw.to_integral_value():
                raise ValueError("input amount has more precision than source decimals")
            normalized["input_amount_raw"] = str(int(raw))
        except Exception:
            return None, ["input_amount_raw"]
    request = {
        "source_asset": {
            "chain": normalized["source_chain"],
            **(
                {"chain_id": normalized["source_chain_id"]}
                if normalized.get("source_chain_id") is not None
                else {}
            ),
            "symbol": normalized["source_symbol"],
            "decimals": int(normalized["source_decimals"]),
            "address": normalized["source_token_address"],
            **(
                {"name": normalized["source_name"]}
                if normalized.get("source_name") is not None
                else {}
            ),
            **(
                {"logo_url": normalized["source_logo_url"]}
                if normalized.get("source_logo_url") is not None
                else {}
            ),
        },
        "destination_asset": {
            "chain": normalized["destination_chain"],
            **(
                {"chain_id": normalized["destination_chain_id"]}
                if normalized.get("destination_chain_id") is not None
                else {}
            ),
            "symbol": normalized["destination_symbol"],
            "decimals": int(normalized["destination_decimals"]),
            "address": normalized["destination_token_address"],
            **(
                {"name": normalized["destination_name"]}
                if normalized.get("destination_name") is not None
                else {}
            ),
            **(
                {"logo_url": normalized["destination_logo_url"]}
                if normalized.get("destination_logo_url") is not None
                else {}
            ),
        },
        "input_amount": str(normalized["input_amount"]),
        "input_amount_raw": normalized["input_amount_raw"],
        "sender_address": normalized["sender_address"],
        "recipient_address": normalized["recipient_address"],
    }
    for field in ("refund_address", "slippage_bps", "expires_at"):
        if normalized.get(field) is not None:
            request[field] = normalized[field]
    try:
        return SwapQuoteRequest.model_validate(request), []
    except Exception:
        return None, ["swap_request"]


async def _resolve_swap_assets(
    draft: dict[str, Any], providers: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fill token metadata when a provider has exactly one matching asset."""
    resolved = dict(draft)
    candidates: list[dict[str, Any]] = []
    for side in ("source", "destination"):
        chain = resolved.get(f"{side}_chain")
        symbol = resolved.get(f"{side}_symbol")
        if not chain or not symbol:
            continue
        if resolved.get(f"{side}_token_address") and resolved.get(f"{side}_decimals") is not None:
            continue
        matches: list[Asset] = []
        query = AssetQuery(chain=str(chain), search=str(symbol))
        for provider in providers.values():
            if hasattr(provider, "list_assets"):
                try:
                    matches.extend(await provider.list_assets(query))
                except Exception:
                    continue
        unique = {
            (str(asset.address).lower(), int(asset.decimals)): asset
            for asset in matches
            if asset.address
            and str(asset.chain).upper() == str(chain).upper()
            and str(asset.symbol).upper() == str(symbol).upper()
        }
        if len(unique) == 1:
            asset = next(iter(unique.values()))
            resolved[f"{side}_token_address"] = asset.address
            resolved[f"{side}_decimals"] = asset.decimals
        elif len(unique) > 1:
            candidates.extend(
                {
                    "side": side,
                    "chain": asset.chain,
                    "symbol": asset.symbol,
                    "address": asset.address,
                    "decimals": asset.decimals,
                }
                for asset in unique.values()
            )
    return resolved, candidates


def make_nodes(runtime: GraphRuntime) -> dict[str, Any]:
    async def wallet_balance_tool(
        chain: str,
        address: str,
    ) -> dict[str, Any]:
        """Read native and token balances for a connected wallet."""
        adapter = runtime.chains.get(str(chain).upper())
        if adapter is None:
            return {
                "ok": False,
                "error": f"当前暂不支持 {chain} 链的余额查询。",
                "code": "CHAIN_CAPABILITY_UNAVAILABLE",
            }
        try:
            if hasattr(adapter, "validate_address") and not await adapter.validate_address(address):
                return {"ok": False, "error": "钱包地址格式无效。", "code": "INVALID_ADDRESS"}
            result: dict[str, Any] = {"address": address, "chain": chain}
            if hasattr(adapter, "get_native_balance"):
                result["native_balance"] = _dump(await adapter.get_native_balance(address))
            if hasattr(adapter, "get_token_balances"):
                result["token_balances"] = [
                    _dump(item) for item in await adapter.get_token_balances(address)
                ]
            return {"ok": True, "wallet": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "code": "WALLET_QUERY_FAILED"}

    wallet_tool_node = ToolNode([wallet_balance_tool], name="wallet_tools")

    async def transaction_status_tool(chain: str, tx_hash: str) -> dict[str, Any]:
        """Read a transaction status from the configured chain adapter."""
        adapter = runtime.chains.get(str(chain).upper())
        if adapter is None:
            return {
                "ok": False,
                "code": "CHAIN_CAPABILITY_UNAVAILABLE",
                "error": f"当前暂不支持 {chain}。",
            }
        try:
            status = await adapter.get_transaction_status(tx_hash)
            value = getattr(status, "value", str(status))
            messages = {
                "pending": "交易已提交，正在等待链上确认。",
                "confirmed": "交易已确认。",
                "failed": "交易执行失败或已回滚。",
                "dropped": "交易可能已被节点丢弃。",
                "unknown": "暂时无法确定交易状态。",
            }
            return {
                "ok": True,
                "transaction": {
                    "chain": str(chain).upper(),
                    "tx_hash": tx_hash,
                    "status": value,
                    "message": messages.get(value, messages["unknown"]),
                },
            }
        except Exception as exc:
            return {"ok": False, "code": "TRANSACTION_STATUS_FAILED", "error": str(exc)}

    async def gas_estimate_tool(
        chain: str,
        address: str,
        to: str | None = None,
        data: str | None = None,
    ) -> dict[str, Any]:
        """Read a network fee estimate without signing or broadcasting."""
        adapter = runtime.chains.get(str(chain).upper())
        if adapter is None:
            return {
                "ok": False,
                "code": "CHAIN_CAPABILITY_UNAVAILABLE",
                "error": f"当前暂不支持 {chain}。",
            }
        if not hasattr(adapter, "estimate_fee"):
            return {
                "ok": False,
                "code": "GAS_ESTIMATE_UNAVAILABLE",
                "error": "当前链暂不支持手续费估算。",
            }
        try:
            fee = await adapter.estimate_fee(to=to, data=data)
            native = await adapter.get_native_balance(address)
            balance_raw = int(native.amount_raw)
            fee_raw = int(fee.amount_raw)
            shortfall_raw = max(fee_raw - balance_raw, 0)
            return {
                "ok": True,
                "gas": {
                    "address": address,
                    "chain": str(chain).upper(),
                    "fee_estimate": _fee_dump(fee),
                    "native_balance": _dump(native),
                    "sufficient": balance_raw >= fee_raw,
                    "shortfall_raw": str(shortfall_raw),
                    "message": (
                        "当前原生币余额足够支付预计网络手续费。"
                        if balance_raw >= fee_raw
                        else "当前原生币余额不足以支付预计网络手续费。"
                    ),
                },
            }
        except Exception as exc:
            return {"ok": False, "code": "GAS_ESTIMATE_FAILED", "error": str(exc)}

    async def asset_discovery_tool(
        chain: str | None = None,
        search: str | None = None,
        provider_name: str | None = None,
    ) -> dict[str, Any]:
        """Read and merge token metadata from configured providers."""
        try:
            query = AssetQuery(chain=chain, search=search)
        except Exception as exc:
            return {"ok": False, "code": "INVALID_ASSET_QUERY", "error": str(exc)}
        selected = (
            {str(provider_name): runtime.providers.get(str(provider_name))}
            if provider_name
            else runtime.providers
        )
        assets: list[Asset] = []
        provider_errors: list[dict[str, Any]] = []
        for name, provider in selected.items():
            if provider is None or not hasattr(provider, "list_assets"):
                continue
            try:
                assets.extend(await provider.list_assets(query))
            except Exception as exc:
                provider_errors.append(
                    _error(
                        "ASSET_DISCOVERY_FAILED",
                        str(exc),
                        retryable=True,
                        details={"provider": name},
                    )
                )
        merged: dict[tuple[str, str, str], Asset] = {}
        for asset in assets:
            key = (
                str(asset.chain).upper(),
                str(asset.symbol).upper(),
                str(asset.address or "").lower(),
            )
            merged[key] = asset
        return {
            "ok": True,
            "query": query.model_dump(mode="json"),
            "assets": [_dump(asset) for asset in merged.values()],
            "providers": list(selected),
            "provider_errors": provider_errors,
        }

    async def quote_lookup_tool(
        provider_name: str,
        swap_request: dict[str, Any],
    ) -> dict[str, Any]:
        """Read one normalized quote; it never prepares or signs a transaction."""
        provider = runtime.providers.get(str(provider_name))
        if provider is None:
            return {
                "ok": False,
                "code": "PROVIDER_UNAVAILABLE",
                "error": f"Provider {provider_name} is unavailable.",
            }
        try:
            quote = await provider.quote(_request(swap_request))
            return {"ok": True, "quote": _dump(quote)}
        except Exception as exc:
            return {
                "ok": False,
                "code": "PROVIDER_QUOTE_FAILED",
                "error": str(exc),
                "provider": str(provider_name),
            }

    transaction_tool_node = ToolNode([transaction_status_tool], name="transaction_tools")
    gas_tool_node = ToolNode([gas_estimate_tool], name="gas_tools")
    asset_tool_node = ToolNode([asset_discovery_tool], name="asset_tools")
    quote_tool_node = ToolNode([quote_lookup_tool], name="quote_tools")

    async def supervisor(state: dict[str, Any]) -> dict[str, Any]:
        """Plan one turn; execution remains in the existing graph nodes."""
        request = _mapping(state.get("request"))
        forced = state.get("forced_intent")
        if forced in _VALID_INTENTS:
            decision = {"intent": forced, "source": "api"}
        elif (
            state.get("intent") in _VALID_INTENTS
            and (
                not request.get("message")
                or (state.get("confirmation_state") or {}).get("status") == "requested"
            )
        ):
            decision = {"intent": state["intent"], "source": "graph_resume"}
        else:
            # Models receive a JSON-safe view, while the original structured
            # request remains untouched in checkpoint state for legacy callers.
            model_request = dict(request)
            # A legacy API caller may still put a Pydantic SwapQuoteRequest
            # under request.metadata (or request.swap_request).  Give the
            # model only JSON values, without mutating the original fields.
            if "swap_request" in model_request:
                model_request["swap_request"] = _dump(model_request["swap_request"])
            model_request["wallet_context"] = state.get("wallet_context")
            model_request["conversation_state"] = state.get("conversation_state")
            model_request["swap_draft"] = state.get("swap_draft")
            model_request["transfer_draft"] = state.get("transfer_draft")
            model_request["available_capabilities"] = {
                "read_tools": [
                    "wallet_balance",
                    "transaction_status",
                    "gas_estimate",
                    "asset_discovery",
                    "quote_lookup",
                ],
                "business_nodes": ["transfer", "swap_allowance", "prepare", "status_poll"],
            }
            try:
                output = (
                    runtime.model.ainvoke(model_request)
                    if hasattr(runtime.model, "ainvoke")
                    else runtime.model.invoke(model_request)
                )
                output = await output if hasattr(output, "__await__") else output
                decision = _parse_model_output(output)
            except Exception as exc:
                decision = {
                    "intent": "clarification",
                    "error": _error("SUPERVISOR_FAILED", str(exc), retryable=True),
                }
            if not decision or decision.get("intent") not in _VALID_INTENTS:
                decision = {
                    "intent": "clarification",
                    "error": _error(
                        "SUPERVISOR_OUTPUT_INVALID",
                        "无法判断这次请求应该执行哪项钱包能力。",
                    ),
                }
            else:
                decision = {**decision, "source": "model"}
        update: dict[str, Any] = {
            "intent": decision["intent"],
            "supervisor_decision": decision,
            "supervisor_output": decision,
            # Preserve every legacy request field while making the checkpoint
            # representation serializable for subsequent resume calls.
            "request": request,
        }
        # Checkpoints created before the Supervisor stored Pydantic requests
        # directly.  Normalize them at this boundary while retaining the
        # original request fields for the legacy graph routes.
        if state.get("swap_request") is not None:
            try:
                update["swap_request"] = _request_json(state["swap_request"])
            except Exception:
                update["swap_request"] = _dump(state["swap_request"])
        for field in (
            "transfer_request",
            "transaction_query",
            "portfolio_request",
            "gas_request",
            "asset_query",
            "price_request",
        ):
            if state.get(field) is not None:
                update[field] = _dump(state[field])
        return update

    async def wallet_tool_call(state: dict[str, Any]) -> dict[str, Any]:
        context = state.get("wallet_context") or {}
        request = state.get("request") or {}
        chain = context.get("chain") or request.get("chain")
        address = context.get("address") or request.get("address")
        if not chain or not address:
            return {
                "tool_result": {
                    "ok": False,
                    "error": "请先连接钱包，我才能查询当前钱包余额。",
                    "code": "WALLET_CONTEXT_REQUIRED",
                }
            }
        tool_call = {
            "name": "wallet_balance_tool",
            "args": {"chain": str(chain), "address": str(address)},
            "id": "wallet-balance-1",
            "type": "tool_call",
        }
        result = await wallet_tool_node.ainvoke(
            {"messages": [AIMessage(content="", tool_calls=[tool_call])]}
        )
        message = result["messages"][-1]
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            payload = {"ok": False, "error": str(message.content), "code": "TOOL_OUTPUT_INVALID"}
        return {"tool_result": payload}

    async def wallet_tool_result(state: dict[str, Any]) -> dict[str, Any]:
        result = state.get("tool_result") or {}
        if not result.get("ok"):
            return {
                "response": {
                    "kind": "clarification"
                    if result.get("code") == "WALLET_CONTEXT_REQUIRED"
                    else "error",
                    "message": result.get("error"),
                    "errors": (
                        []
                        if result.get("code") == "WALLET_CONTEXT_REQUIRED"
                        else [
                            _error(
                                result.get("code", "TOOL_FAILED"),
                                result.get("error", "工具调用失败。"),
                            )
                        ]
                    ),
                }
            }
        wallet = result.get("wallet") or {}
        return {
            "wallet_context": wallet,
            "response": {"kind": "wallet_query", "wallet": wallet},
        }

    async def transaction_tool_call(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("transaction_query") or {}
        chain = (
            raw.get("chain")
            or raw.get("transaction_chain")
            or state.get("request", {}).get("chain")
        )
        tx_hash = raw.get("tx_hash") or raw.get("transaction_hash")
        if not chain or not tx_hash:
            return {
                "tool_result": {
                    "ok": False,
                    "code": "TRANSACTION_PARAMETERS_REQUIRED",
                    "error": "请提供交易所在的链和交易哈希。",
                }
            }
        call = {
            "name": "transaction_status_tool",
            "args": {"chain": str(chain), "tx_hash": str(tx_hash)},
            "id": "transaction-status-1",
            "type": "tool_call",
        }
        result = await transaction_tool_node.ainvoke(
            {"messages": [AIMessage(content="", tool_calls=[call])]}
        )
        try:
            return {"tool_result": json.loads(result["messages"][-1].content)}
        except (TypeError, ValueError):
            return {
                "tool_result": {
                    "ok": False,
                    "code": "TOOL_OUTPUT_INVALID",
                    "error": str(result["messages"][-1].content),
                }
            }

    async def transaction_tool_result(state: dict[str, Any]) -> dict[str, Any]:
        result = state.get("tool_result") or {}
        if not result.get("ok"):
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            result.get("code", "TOOL_FAILED"), result.get("error", "工具调用失败。")
                        )
                    ],
                }
            }
        transaction = result.get("transaction") or {}
        return {
            "transaction_status_snapshot": transaction,
            "response": {"kind": "transaction_status", **transaction},
        }

    async def gas_tool_call(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("gas_request") or {}
        chain = raw.get("chain") or raw.get("gas_chain") or state.get("request", {}).get("chain")
        if not chain:
            return {
                "tool_result": {
                    "ok": False,
                    "code": "GAS_CHAIN_REQUIRED",
                    "error": "请告诉我需要查询哪条链的 Gas。",
                }
            }
        address = (
            raw.get("address")
            or raw.get("gas_address")
            or (state.get("wallet_context") or {}).get("address")
        )
        if not address:
            return {
                "tool_result": {
                    "ok": False,
                    "code": "WALLET_CONTEXT_REQUIRED",
                    "error": "请先连接钱包，或提供钱包地址。",
                }
            }
        call = {
            "name": "gas_estimate_tool",
            "args": {
                "chain": str(chain),
                "address": str(address),
                "to": raw.get("to") or raw.get("gas_to"),
                "data": raw.get("data") or raw.get("gas_data"),
            },
            "id": "gas-estimate-1",
            "type": "tool_call",
        }
        result = await gas_tool_node.ainvoke(
            {"messages": [AIMessage(content="", tool_calls=[call])]}
        )
        try:
            return {"tool_result": json.loads(result["messages"][-1].content)}
        except (TypeError, ValueError):
            return {
                "tool_result": {
                    "ok": False,
                    "code": "TOOL_OUTPUT_INVALID",
                    "error": str(result["messages"][-1].content),
                }
            }

    async def gas_tool_result(state: dict[str, Any]) -> dict[str, Any]:
        result = state.get("tool_result") or {}
        if not result.get("ok"):
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            result.get("code", "TOOL_FAILED"), result.get("error", "工具调用失败。")
                        )
                    ],
                }
            }
        gas = result.get("gas") or {}
        return {"gas_snapshot": gas, "response": {"kind": "gas_check", **gas}}

    async def confirmation_request(state: dict[str, Any]) -> dict[str, Any]:
        selected = _dump(state.get("selected_quote"))
        if not isinstance(selected, dict):
            selected = None
        if selected is None:
            return {
                "response": {
                    "kind": "clarification",
                    "message": "请先选择一个兑换报价。",
                }
            }
        current = state.get("confirmation_state") or {}
        if current.get("status") == "approved":
            return {"task_stage": "confirmed"}
        if current.get("status") == "requested" and not _confirmation_expired(current):
            return {"task_stage": "awaiting_confirmation"}
        confirmation = _confirmation_snapshot(
            action="swap",
            status="requested",
            summary={
                "provider": selected.get("provider"),
                "provider_reference": selected.get("provider_reference"),
                "source_asset": selected.get("source_asset"),
                "destination_asset": selected.get("destination_asset"),
                "input_amount": selected.get("input_amount"),
                "expected_output": selected.get("expected_output"),
            },
            ttl_seconds=runtime.confirmation_ttl_seconds,
        )
        return {
            "confirmation_state": confirmation,
            "task_stage": "awaiting_confirmation",
            "response": {
                "kind": "confirmation_required",
                "confirmation": confirmation,
                "quote": selected,
            },
        }

    async def confirmation_wait(state: dict[str, Any]) -> dict[str, Any]:
        current = state.get("confirmation_state") or {}
        if current.get("status") == "approved":
            return {}
        if _confirmation_expired(current):
            expired = {
                **current,
                "status": "expired",
                "reason": "confirmation_timeout",
            }
            return {
                "confirmation_state": expired,
                "task_stage": "confirmation_expired",
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            "CONFIRMATION_EXPIRED", "确认已过期，请重新获取报价。", retryable=True
                        )
                    ],
                    "confirmation": expired,
                },
            }
        answer = interrupt(
            {
                "kind": "confirmation_required",
                "confirmation": current,
                "quote": state.get("selected_quote"),
            }
        )
        approved = isinstance(answer, dict) and bool(answer.get("approved"))
        if answer is True:
            approved = True
        updated = {
            **current,
            "status": "approved" if approved else "rejected",
            "reason": None if approved else "user_rejected",
        }
        if not approved:
            return {
                "confirmation_state": updated,
                "user_confirmation": {"approved": False},
                "task_stage": "cancelled",
                "response": {
                    "kind": "cancelled",
                    "message": "已取消本次兑换。",
                    "confirmation": updated,
                },
            }
        return {
            "confirmation_state": updated,
            "user_confirmation": {"approved": True},
            "task_stage": "confirmed",
        }

    async def asset_tool_call(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("asset_query") or {}
        call = {
            "name": "asset_discovery_tool",
            "args": {
                "chain": raw.get("chain") or raw.get("asset_chain"),
                "search": raw.get("search") or raw.get("asset_search"),
            },
            "id": "asset-discovery-1",
            "type": "tool_call",
        }
        result = await asset_tool_node.ainvoke(
            {"messages": [AIMessage(content="", tool_calls=[call])]}
        )
        try:
            return {"tool_result": json.loads(result["messages"][-1].content)}
        except (TypeError, ValueError):
            return {
                "tool_result": {
                    "ok": False,
                    "code": "TOOL_OUTPUT_INVALID",
                    "error": str(result["messages"][-1].content),
                }
            }

    async def asset_tool_result(state: dict[str, Any]) -> dict[str, Any]:
        result = state.get("tool_result") or {}
        if not result.get("ok"):
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            result.get("code", "TOOL_FAILED"), result.get("error", "资产查询失败。")
                        )
                    ],
                }
            }
        snapshot = {
            "query": result.get("query") or {},
            "assets": result.get("assets") or [],
            "providers": result.get("providers") or [],
            "provider_errors": result.get("provider_errors") or [],
        }
        return {
            "asset_snapshot": snapshot,
            "response": {"kind": "asset_discovery", **snapshot},
        }

    async def quote_tool_call(state: dict[str, Any]) -> dict[str, Any]:
        name = state.get("provider_name") or state.get("request", {}).get("_provider_name")
        request = state.get("swap_request")
        if not name or not request:
            return {
                "quote_candidates": [],
                "errors": [_error("QUOTE_PARAMETERS_REQUIRED", "兑换报价参数不完整。")],
            }
        try:
            request_json = _request_json(request)
        except Exception as exc:
            return {
                "quote_candidates": [],
                "errors": [_error("INVALID_SWAP_PARAMETERS", str(exc))],
            }
        call = {
            "name": "quote_lookup_tool",
            "args": {"provider_name": str(name), "swap_request": request_json},
            "id": f"quote-{name}",
            "type": "tool_call",
        }
        result = await quote_tool_node.ainvoke(
            {"messages": [AIMessage(content="", tool_calls=[call])]}
        )
        try:
            payload = json.loads(result["messages"][-1].content)
        except (TypeError, ValueError):
            payload = {
                "ok": False,
                "code": "TOOL_OUTPUT_INVALID",
                "error": str(result["messages"][-1].content),
            }
        if not payload.get("ok"):
            return {
                "quote_candidates": [],
                "errors": [
                    _error(
                        payload.get("code", "TOOL_FAILED"),
                        payload.get("error", "报价查询失败。"),
                        details={"provider": name},
                    )
                ],
            }
        return {"quote_candidates": [payload["quote"]]}

    async def intent(state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("forced_intent")
        request = _mapping(state.get("request"))
        if existing is None and not request.get("message"):
            existing = state.get("intent")
        valid = {
            "wallet_query",
            "swap_quote",
            "swap_prepare",
            "swap_status",
            "clarification",
            "unsupported",
            "transfer",
            "swap_select",
            "swap_allowance",
            "price_query",
            "transaction_status",
            "portfolio_query",
            "gas_check",
            "asset_discovery",
        }
        supervisor_source = (state.get("supervisor_output") or {}).get("source")
        if existing in valid and (
            not state.get("supervisor_output") or supervisor_source in {"api", "graph_resume"}
        ):
            return {"route": existing, "max_poll_attempts": runtime.max_poll_attempts}
        message = str(request.get("message", ""))
        if _looks_like_cancel_message(message) and (
            state.get("conversation_state")
            or state.get("swap_draft")
            or state.get("swap_request")
            or state.get("selected_quote")
        ):
            cancelled = _task_state(
                state.get("conversation_state"),
                goal="swap",
                stage="cancelled",
                slots={},
                missing_fields=[],
                status="cancelled",
                updated_by="user",
            )
            return {
                "intent": "clarification",
                "forced_intent": None,
                "route": "response",
                "conversation_state": cancelled,
                "task_stage": "cancelled",
                "swap_draft": {},
                "swap_request": None,
                "token_candidates": [],
                "quote_candidates": [{"__clear__": True}],
                "selected_quote": None,
                "pending_transaction": None,
                "user_confirmation": None,
                "response": {"kind": "cancelled", "message": "已取消当前兑换。"},
            }
        parsed = _parse_model_output(state.get("supervisor_output"))
        if parsed and parsed.get("source") in {"api", "graph_resume"}:
            # Forced/resumed requests may already contain structured fields;
            # reconstruct a minimal model output from the preserved request.
            parsed = {**parsed, "intent": existing or parsed.get("intent")}
        if not parsed:
            try:
                model_request = dict(request)
                model_request["wallet_context"] = state.get("wallet_context")
                model_request["swap_draft"] = state.get("swap_draft")
                model_request["transfer_draft"] = state.get("transfer_draft")
                model_request["conversation_state"] = state.get("conversation_state")
                output = (
                    runtime.model.ainvoke(model_request)
                    if hasattr(runtime.model, "ainvoke")
                    else runtime.model.invoke(model_request)
                )
                output = await output if hasattr(output, "__await__") else output
                parsed = _parse_model_output(output)
            except Exception as exc:  # model failures are user-actionable clarification
                return {
                    "intent": "clarification",
                    "errors": [_error("MODEL_OUTPUT_INVALID", str(exc))],
                    "route": "clarification",
                    "max_poll_attempts": runtime.max_poll_attempts,
                }
        if not parsed or parsed.get("intent") not in valid:
            return {
                "intent": "clarification",
                "errors": [
                    _error(
                        "MODEL_OUTPUT_INVALID",
                        "The assistant could not determine the requested action.",
                    )
                ],
                "route": "clarification",
                "max_poll_attempts": runtime.max_poll_attempts,
            }
        intent_value = parsed["intent"]
        if intent_value == "clarification":
            looks_like_swap = _looks_like_swap_message(message)
            state_update = {}
            if looks_like_swap and (state.get("swap_draft") or state.get("conversation_state")):
                state_update = _task_update(
                    state.get("conversation_state"),
                    goal="swap",
                    stage="collecting_parameters",
                    slots=state.get("swap_draft") or {},
                    missing_fields=parsed.get("missing_fields")
                    or state.get("missing_fields")
                    or [],
                )
            return {
                "intent": "clarification",
                "route": "clarification",
                "max_poll_attempts": runtime.max_poll_attempts,
                **state_update,
                "response": {
                    "kind": "clarification",
                    "message": _clarification_message(
                        message=parsed.get("message"),
                        swap=looks_like_swap,
                        missing=parsed.get("missing_fields"),
                    ),
                    "missing_fields": parsed.get("missing_fields") or [],
                },
            }
        if intent_value == "swap_quote":
            draft = dict(state.get("swap_draft") or {})
            if not draft and state.get("swap_request"):
                draft = _swap_draft_from_request(state.get("swap_request"))
            incoming = {key: parsed[key] for key in _SWAP_DRAFT_KEYS if parsed.get(key) is not None}
            previous_draft = dict(draft)
            draft = _merge_swap_slots(draft, incoming)
            changed = draft != previous_draft
            draft, token_candidates = await _resolve_swap_assets(draft, runtime.providers)
            if token_candidates:
                descriptions = [
                    (
                        f"{'来源' if item['side'] == 'source' else '目标'} "
                        f"{item['symbol']} ({item['chain']})：{item['address']}"
                    )
                    for item in token_candidates
                ]
                return {
                    "intent": "clarification",
                    "route": "clarification",
                    "swap_draft": draft,
                    "token_candidates": token_candidates,
                    "missing_fields": [],
                    **_task_update(
                        state.get("conversation_state"),
                        goal="swap",
                        stage="selecting_token",
                        slots=draft,
                        missing_fields=[],
                    ),
                    "max_poll_attempts": runtime.max_poll_attempts,
                    "response": {
                        "kind": "clarification",
                        "message": (
                            "我找到了多个同名 Token，不能直接替你猜地址。"
                            f"请确认你要使用哪一个：{'；'.join(descriptions)}。"
                        ),
                        "token_candidates": token_candidates,
                    },
                }
            request_model, missing = _swap_draft_request(draft, state.get("wallet_context"))
            if request_model is None:
                return {
                    "intent": "clarification",
                    "route": "clarification",
                    "swap_draft": draft,
                    "missing_fields": missing,
                    **_task_update(
                        state.get("conversation_state"),
                        goal="swap",
                        stage="collecting_parameters",
                        slots=draft,
                        missing_fields=missing,
                    ),
                    "max_poll_attempts": runtime.max_poll_attempts,
                    "response": {
                        "kind": "clarification",
                        "message": _clarification_message(missing=missing, swap=True),
                        "missing_fields": missing,
                    },
                }
            return {
                "intent": intent_value,
                "route": intent_value,
                "swap_draft": draft,
                "missing_fields": [],
                "swap_request": request_model.model_dump(mode="json"),
                **(
                    {"selected_quote": None, "quote_candidates": [{"__clear__": True}]}
                    if changed
                    else {}
                ),
                **_task_update(
                    state.get("conversation_state"),
                    goal="swap",
                    stage="ready_for_quote",
                    slots=draft,
                    missing_fields=[],
                ),
                "max_poll_attempts": runtime.max_poll_attempts,
            }
        if intent_value == "transfer":
            draft = dict(state.get("transfer_draft") or {})
            draft.update(
                {key: parsed[key] for key in _TRANSFER_DRAFT_KEYS if parsed.get(key) is not None}
            )
            request_model, missing = _transfer_draft_request(draft, state.get("wallet_context"))
            if request_model is None:
                labels = ", ".join(missing)
                return {
                    "intent": "clarification",
                    "route": "clarification",
                    "transfer_draft": draft,
                    "missing_fields": missing,
                    **_task_update(
                        state.get("conversation_state"),
                        goal="transfer",
                        stage="collecting_parameters",
                        slots=draft,
                        missing_fields=missing,
                    ),
                    "max_poll_attempts": runtime.max_poll_attempts,
                    "response": {
                        "kind": "clarification",
                        "message": f"还需要这些转账信息：{labels}。",
                        "missing_fields": missing,
                    },
                }
            return {
                "intent": intent_value,
                "route": intent_value,
                "transfer_draft": draft,
                "missing_fields": [],
                "transfer_request": request_model.model_dump(mode="json"),
                **_task_update(
                    state.get("conversation_state"),
                    goal="transfer",
                    stage="ready_for_prepare",
                    slots=draft,
                    missing_fields=[],
                ),
                "max_poll_attempts": runtime.max_poll_attempts,
            }
        if intent_value == "transaction_status":
            draft = dict(state.get("transaction_query") or {})
            draft.update(
                {key: parsed[key] for key in _TRANSACTION_QUERY_KEYS if parsed.get(key) is not None}
            )
            draft.setdefault("chain", draft.pop("transaction_chain", None))
            draft.setdefault("tx_hash", draft.pop("transaction_hash", None))
            missing = [field for field in ("chain", "tx_hash") if not draft.get(field)]
            if missing:
                return {
                    "intent": "clarification",
                    "route": "clarification",
                    "transaction_query": draft,
                    "missing_fields": [f"transaction_{field}" for field in missing],
                    "response": {
                        "kind": "clarification",
                        "message": "请提供交易所在链和交易哈希。",
                        "missing_fields": [f"transaction_{field}" for field in missing],
                    },
                }
            return {
                "intent": intent_value,
                "route": intent_value,
                "transaction_query": draft,
                "max_poll_attempts": runtime.max_poll_attempts,
            }
        if intent_value == "portfolio_query":
            draft = dict(state.get("portfolio_request") or {})
            draft.update(
                {key: parsed[key] for key in _PORTFOLIO_QUERY_KEYS if parsed.get(key) is not None}
            )
            draft.setdefault("chain", draft.pop("portfolio_chain", None))
            if not draft.get("chain"):
                draft["chain"] = (state.get("wallet_context") or {}).get("chain")
            if not draft.get("chain") or not (state.get("wallet_context") or {}).get("address"):
                missing = ["portfolio_chain"] if not draft.get("chain") else ["wallet_address"]
                return {
                    "intent": "clarification",
                    "route": "clarification",
                    "portfolio_request": draft,
                    "missing_fields": missing,
                    "response": {
                        "kind": "clarification",
                        "message": "请提供钱包所在链和已连接的钱包地址。",
                        "missing_fields": missing,
                    },
                }
            return {
                "intent": intent_value,
                "route": intent_value,
                "portfolio_request": draft,
                "max_poll_attempts": runtime.max_poll_attempts,
            }
        if intent_value == "gas_check":
            draft = dict(state.get("gas_request") or {})
            draft.update(
                {key: parsed[key] for key in _GAS_QUERY_KEYS if parsed.get(key) is not None}
            )
            draft.setdefault("chain", draft.pop("gas_chain", None))
            if not draft.get("chain"):
                draft["chain"] = (state.get("wallet_context") or {}).get("chain")
            if not draft.get("chain"):
                return {
                    "intent": "clarification",
                    "route": "clarification",
                    "gas_request": draft,
                    "missing_fields": ["gas_chain"],
                    "response": {
                        "kind": "clarification",
                        "message": "请提供需要检查手续费的链。",
                        "missing_fields": ["gas_chain"],
                    },
                }
            return {
                "intent": intent_value,
                "route": intent_value,
                "gas_request": draft,
                "max_poll_attempts": runtime.max_poll_attempts,
            }
        if intent_value == "asset_discovery":
            draft = dict(state.get("asset_query") or {})
            draft.update(
                {key: parsed[key] for key in _ASSET_QUERY_KEYS if parsed.get(key) is not None}
            )
            draft.setdefault("chain", draft.pop("asset_chain", None))
            draft.setdefault("search", draft.pop("asset_search", None))
            if not draft.get("chain") and not draft.get("search"):
                return {
                    "intent": "clarification",
                    "route": "clarification",
                    "asset_query": draft,
                    "missing_fields": ["asset_chain", "asset_search"],
                    "response": {
                        "kind": "clarification",
                        "message": "请提供要搜索的链或 Token 名称。",
                        "missing_fields": ["asset_chain", "asset_search"],
                    },
                }
            return {
                "intent": intent_value,
                "route": intent_value,
                "asset_query": draft,
                "max_poll_attempts": runtime.max_poll_attempts,
            }
        return {
            "intent": intent_value,
            "route": intent_value,
            "max_poll_attempts": runtime.max_poll_attempts,
        }

    async def resolve_swap(state: dict[str, Any]) -> dict[str, Any]:
        request_context = _mapping(state.get("request"))
        raw = state.get("swap_request") or request_context.get("swap_request")
        # Older callers passed SwapQuoteRequest itself as ``request``. Keep
        # that shape readable while canonicalizing only the graph field.
        if raw is None and "source_asset" in request_context:
            raw = request_context
        if raw is None:
            return {
                "intent": "clarification",
                "errors": [_error("MISSING_SWAP_PARAMETERS", "Swap parameters are required.")],
            }
        try:
            request = _request(raw)
        except Exception as exc:
            return {
                "intent": "clarification",
                "errors": [_error("INVALID_SWAP_PARAMETERS", str(exc))],
            }
        selected = state.get("selected_quote")
        if state.get("intent") == "swap_quote" and not runtime.providers:
            return {
                "swap_request": request.model_dump(mode="json"),
                "available_providers": [],
                "selected_quote": selected,
                "errors": [
                    _error(
                        "PROVIDER_UNAVAILABLE",
                        "没有可用的兑换 Provider。",
                        details={"capability": "quote"},
                    )
                ],
            }
        return {
            "swap_request": request.model_dump(mode="json"),
            "available_providers": list(runtime.providers),
            "selected_quote": selected,
        }

    async def quote_provider(state: dict[str, Any]) -> dict[str, Any]:
        return await quote_tool_call(state)

    async def quote_response(state: dict[str, Any]) -> dict[str, Any]:
        quotes = [_dump(item) for item in state.get("quote_candidates", [])]
        quotes = [item for item in quotes if isinstance(item, dict)]
        if not quotes and state.get("errors"):
            return {"response": {"kind": "error", "errors": state["errors"]}}
        # Never silently choose a provider when multiple candidates exist.
        selected = quotes[0] if len(quotes) == 1 else _dump(state.get("selected_quote"))
        if runtime.price_provider and quotes:
            assets = []
            for item in quotes:
                assets.extend([item.get("source_asset"), item.get("destination_asset")])
            try:
                from wallet_agent.domain.models import Asset

                parsed_assets = [Asset.model_validate(a) for a in assets if a]
                prices = await runtime.price_provider.get_prices(parsed_assets)
                price_map = {(p.asset.chain, p.asset.symbol, p.asset.address): p for p in prices}
                enriched = []
                for item in quotes:
                    q = dict(item)
                    source = q.get("source_asset", {})
                    destination = q.get("destination_asset", {})
                    src = price_map.get(
                        (source.get("chain"), source.get("symbol"), source.get("address"))
                    )
                    dst = price_map.get(
                        (
                            destination.get("chain"),
                            destination.get("symbol"),
                            destination.get("address"),
                        )
                    )
                    snaps = [p.model_dump(mode="json") for p in (src, dst) if p]
                    q["price_snapshots"] = snaps
                    if src:
                        q["usd_input_value"] = str(
                            Decimal(str(q.get("input_amount", 0))) * src.usd_price
                        )
                    if dst:
                        q["usd_expected_output"] = str(
                            Decimal(str(q.get("expected_output", 0))) * dst.usd_price
                        )
                    enriched.append(q)
                quotes = enriched
            except Exception:
                pass
        snapshot_map: dict[str, Any] = {}
        for item in quotes:
            raw_snapshots = item.get("price_snapshots") or []
            values = raw_snapshots.values() if isinstance(raw_snapshots, dict) else raw_snapshots
            for snapshot in values:
                snap = _dump(snapshot)
                if not isinstance(snap, dict):
                    continue
                asset = snap.get("asset") or {}
                key = ":".join(
                    str(asset.get(part) or "")
                    for part in ("chain", "chain_id", "symbol", "address")
                )
                snapshot_map[key] = snap
        stage = "selecting_quote" if len(quotes) > 1 else "ready_for_prepare"
        task_update = _task_update(
            state.get("conversation_state"),
            goal="swap",
            stage=stage,
            slots=state.get("swap_draft") or {},
            missing_fields=[],
        )
        return {
            "selected_quote": selected,
            "quote_candidates": quotes,
            **task_update,
            "response": {
                "kind": "swap_quote",
                "quotes": quotes,
                "price_snapshots": snapshot_map,
            },
        }

    async def transfer(state: dict[str, Any]) -> dict[str, Any]:
        raw = (
            state.get("request", {}).get("transfer_request")
            or state.get("transfer_request")
            or state.get("request", {})
        )
        try:
            req = TransferRequest.model_validate(raw)
        except Exception as exc:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("INVALID_TRANSFER_PARAMETERS", str(exc))],
                }
            }
        adapter = runtime.chains.get(req.chain.upper())
        if adapter is None:
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error("CHAIN_CAPABILITY_UNAVAILABLE", f"Chain {req.chain} is unavailable.")
                    ],
                }
            }
        try:
            if req.token is None:
                tx = adapter.build_native_transfer(
                    from_address=req.sender, to_address=req.recipient, amount_raw=req.amount_raw
                )
            else:
                tx = adapter.build_erc20_transfer(
                    token=req.token,
                    from_address=req.sender,
                    to_address=req.recipient,
                    amount_raw=req.amount_raw,
                )
            preflight = await _transaction_preflight(
                adapter=adapter,
                chain=req.chain,
                sender=req.sender,
                recipient=req.recipient,
                amount_raw=req.amount_raw,
                token=req.token,
                transaction=tx,
                wallet_context=state.get("wallet_context"),
            )
            if not preflight["ok"]:
                return {
                    "preflight": preflight,
                    "response": {
                        "kind": "error",
                        "errors": [
                            _error(
                                "TRANSACTION_PREFLIGHT_FAILED",
                                "交易预检查未通过。",
                                details={"preflight": preflight},
                            )
                        ],
                        "preflight": preflight,
                    },
                }
            return {
                "preflight": preflight,
                "pending_transaction": tx.model_dump(mode="json"),
                "response": {
                    "kind": "transfer_prepare",
                    "pending_transaction": tx.model_dump(mode="json"),
                    "preflight": preflight,
                },
            }
        except Exception as exc:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("TRANSFER_PREPARE_FAILED", str(exc))],
                }
            }

    async def swap_allowance(state: dict[str, Any]) -> dict[str, Any]:
        selected = state.get("selected_quote")
        if not selected:
            return {
                "response": {
                    "stage": "quote_selection_required",
                    "quotes": state.get("quote_candidates", []),
                }
            }
        quote = _quote(selected)
        requirement = quote.allowance_requirement
        if requirement is None:
            # Native swaps do not need approval; prepare directly.
            provider = runtime.providers.get(str(selected.get("provider")))
            if provider is None:
                return {
                    "response": {
                        "stage": "failed",
                        "errors": [_error("PROVIDER_UNAVAILABLE", "Provider unavailable")],
                    }
                }
            prepared = await provider.prepare(quote)
            dumped = _dump(prepared)
            preflight = None
            if isinstance(prepared, UnsignedTransaction):
                request = state.get("swap_request") or {}
                source_asset = request.get("source_asset") or selected.get("source_asset") or {}
                source_chain = str(source_asset.get("chain") or prepared.chain)
                adapter = runtime.chains.get(source_chain.upper())
                if adapter is not None:
                    preflight = await _transaction_preflight(
                        adapter=adapter,
                        chain=source_chain,
                        sender=str(request.get("sender_address") or ""),
                        recipient=str(request.get("recipient_address") or ""),
                        amount_raw=str(request.get("input_amount_raw") or "0"),
                        token=(
                            Asset.model_validate(source_asset)
                            if source_asset.get("address")
                            else None
                        ),
                        transaction=prepared,
                        wallet_context=state.get("wallet_context"),
                    )
                    if not preflight["ok"]:
                        return {
                            "preflight": preflight,
                            "response": {
                                "kind": "error",
                                "errors": [
                                    _error(
                                        "TRANSACTION_PREFLIGHT_FAILED",
                                        "交易预检查未通过。",
                                        details={"preflight": preflight},
                                    )
                                ],
                                "preflight": preflight,
                            },
                        }
            return {
                "pending_transaction": dumped,
                "preflight": preflight,
                "authorization_stage": "swap_ready",
                "response": {
                    "stage": "swap_ready",
                    "pending_transaction": dumped,
                    **({"preflight": preflight} if preflight is not None else {}),
                },
            }
        adapter = runtime.chains.get(requirement.token.chain.upper())
        if adapter is None:
            return {
                "response": {
                    "stage": "failed",
                    "errors": [_error("CHAIN_CAPABILITY_UNAVAILABLE", "Chain adapter unavailable")],
                }
            }
        allowance_raw = await adapter.get_allowance(
            requirement.token, requirement.owner, requirement.spender
        )
        required = int(requirement.required_amount_raw)
        approval_tx_hash = state.get("approval_tx_hash")
        approval_transaction = state.get("approval_transaction")
        if int(allowance_raw) < required and not approval_tx_hash:
            approval = adapter.build_erc20_approve(
                token=requirement.token,
                owner=requirement.owner,
                spender=requirement.spender,
                amount_raw=requirement.required_amount_raw,
            )
            approval_model = ApprovalTransaction(
                chain=requirement.token.chain,
                chain_id=requirement.token.chain_id,
                to=approval.to,
                data=approval.data,
                value=approval.value,
                token=requirement.token,
                owner=requirement.owner,
                spender=requirement.spender,
                amount_raw=requirement.required_amount_raw,
            )
            approval_transaction = approval_model.model_dump(mode="json")
            answer = interrupt(
                {
                    "kind": "approval_required",
                    "approval_transaction": approval_model.model_dump(mode="json"),
                }
            )
            if isinstance(answer, dict):
                h = answer.get("approve_tx_hash") or answer.get("tx_hash")
                if h:
                    approval_tx_hash = str(h)
            if not approval_tx_hash:
                return {
                    "approval_transaction": approval_model.model_dump(mode="json"),
                    "allowance_requirement": requirement.model_dump(mode="json"),
                    "authorization_stage": "approval_required",
                    "response": {
                        "stage": "approval_required",
                        "approval_transaction": approval_model.model_dump(mode="json"),
                    },
                }
        if approval_tx_hash:
            receipt = await adapter.get_transaction_receipt(str(approval_tx_hash))
            success = receipt_success(receipt)
            if success is False or receipt is None:
                return {
                    "approval_transaction": approval_transaction,
                    "approval_tx_hash": approval_tx_hash,
                    "allowance_requirement": requirement.model_dump(mode="json"),
                    "response": {
                        "stage": "approval_pending" if receipt is None else "approval_failed",
                        "approval_transaction": approval_transaction,
                    },
                }
            allowance_raw = await adapter.get_allowance(
                requirement.token, requirement.owner, requirement.spender
            )
            if int(allowance_raw) < required:
                return {
                    "approval_transaction": approval_transaction,
                    "approval_tx_hash": approval_tx_hash,
                    "allowance_requirement": requirement.model_dump(mode="json"),
                    "response": {
                        "stage": "approval_required",
                        "approval_transaction": approval_transaction,
                    },
                }
        provider = runtime.providers.get(str(selected.get("provider")))
        if provider is None:
            return {
                "response": {
                    "stage": "failed",
                    "errors": [_error("PROVIDER_UNAVAILABLE", "Provider unavailable")],
                }
            }
        prepared = await provider.prepare(quote)
        dumped = _dump(prepared)
        preflight = None
        if isinstance(prepared, UnsignedTransaction):
            request = state.get("swap_request") or {}
            source_asset = request.get("source_asset") or selected.get("source_asset") or {}
            source_chain = str(source_asset.get("chain") or prepared.chain)
            adapter = runtime.chains.get(source_chain.upper())
            if adapter is not None:
                preflight = await _transaction_preflight(
                    adapter=adapter,
                    chain=source_chain,
                    sender=str(request.get("sender_address") or ""),
                    recipient=str(request.get("recipient_address") or ""),
                    amount_raw=str(request.get("input_amount_raw") or "0"),
                    token=(
                        Asset.model_validate(source_asset) if source_asset.get("address") else None
                    ),
                    transaction=prepared,
                    wallet_context=state.get("wallet_context"),
                )
                if not preflight["ok"]:
                    return {
                        "preflight": preflight,
                        "response": {
                            "kind": "error",
                            "errors": [
                                _error(
                                    "TRANSACTION_PREFLIGHT_FAILED",
                                    "交易预检查未通过。",
                                    details={"preflight": preflight},
                                )
                            ],
                            "preflight": preflight,
                        },
                    }
            return {
                "approval_transaction": approval_transaction,
                "approval_tx_hash": approval_tx_hash,
                "allowance_requirement": requirement.model_dump(mode="json"),
                "pending_transaction": dumped,
                "preflight": preflight,
                "authorization_stage": "swap_ready",
                "response": {
                    "stage": "swap_ready",
                    "pending_transaction": dumped,
                    **({"preflight": preflight} if preflight is not None else {}),
                },
            }

    async def wallet_query(state: dict[str, Any]) -> dict[str, Any]:
        request = state.get("request", {})
        chain = str(request.get("chain", ""))
        address = str(request.get("address", ""))
        adapter = runtime.chains.get(chain.upper())
        if adapter is None:
            return {
                "errors": [
                    _error(
                        "CHAIN_CAPABILITY_UNAVAILABLE",
                        f"Chain {chain} is unavailable.",
                        details={"chain": chain},
                    )
                ]
            }
        try:
            if hasattr(adapter, "validate_address") and not await adapter.validate_address(address):
                return {"errors": [_error("INVALID_ADDRESS", "The wallet address is invalid.")]}
            snapshot = {"address": address, "chain": chain}
            if hasattr(adapter, "get_native_balance"):
                snapshot["native_balance"] = _dump(await adapter.get_native_balance(address))
            if hasattr(adapter, "get_token_balances"):
                snapshot["token_balances"] = [
                    _dump(item) for item in await adapter.get_token_balances(address)
                ]
            return {
                "wallet_context": snapshot,
                "response": {"kind": "wallet_query", "wallet": snapshot},
            }
        except Exception as exc:
            return {"errors": [_error("WALLET_QUERY_FAILED", str(exc))]}

    async def portfolio_query(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("portfolio_request") or {}
        context = state.get("wallet_context") or {}
        chain = str(raw.get("chain") or context.get("chain") or "").upper()
        address = str(raw.get("address") or context.get("address") or "")
        adapter = runtime.chains.get(chain)
        if adapter is None:
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            "CHAIN_CAPABILITY_UNAVAILABLE",
                            f"Chain {chain} is unavailable.",
                            details={"chain": chain},
                        )
                    ],
                }
            }
        if not address:
            return {
                "response": {
                    "kind": "clarification",
                    "message": "请先连接钱包，或提供钱包地址。",
                    "missing_fields": ["wallet_address"],
                }
            }
        try:
            balances = []
            native = await adapter.get_native_balance(address)
            balances.append(native)
            if hasattr(adapter, "get_token_balances"):
                balances.extend(await adapter.get_token_balances(address))
            assets = [_dump(balance) for balance in balances]
            prices: list[Any] = []
            price_status = "unavailable"
            price_error = None
            if runtime.price_provider is not None and balances:
                try:
                    prices = await runtime.price_provider.get_prices(
                        [balance.asset for balance in balances]
                    )
                    price_status = "available"
                except Exception as exc:
                    price_error = str(exc)
            price_map = {
                (
                    str(price.asset.chain).upper(),
                    str(price.asset.chain_id or ""),
                    str(price.asset.symbol).upper(),
                    str(price.asset.address or "").lower(),
                ): price
                for price in prices
            }
            total_usd = Decimal("0")
            has_usd_value = False
            for item, balance in zip(assets, balances, strict=False):
                key = (
                    str(balance.asset.chain).upper(),
                    str(balance.asset.chain_id or ""),
                    str(balance.asset.symbol).upper(),
                    str(balance.asset.address or "").lower(),
                )
                price = price_map.get(key)
                if price is None:
                    price = next(
                        (
                            candidate
                            for candidate in prices
                            if (
                                str(candidate.asset.chain).upper()
                                == str(balance.asset.chain).upper()
                            )
                            and str(candidate.asset.symbol).upper()
                            == str(balance.asset.symbol).upper()
                            and str(candidate.asset.address or "").lower()
                            == str(balance.asset.address or "").lower()
                        ),
                        None,
                    )
                if price is not None:
                    usd_value = balance.amount * price.usd_price
                    item["usd_value"] = str(usd_value)
                    total_usd += usd_value
                    has_usd_value = True
            snapshot: dict[str, Any] = {
                "address": address,
                "chain": chain,
                "chain_id": context.get("chain_id"),
                "assets": assets,
                "total_usd_value": str(total_usd) if has_usd_value else None,
                "price_status": price_status,
                "price_snapshots": [_dump(price) for price in prices],
            }
            if price_error:
                snapshot["price_error"] = "价格服务暂时不可用。"
            return {
                "portfolio_snapshot": snapshot,
                "response": {"kind": "portfolio_query", "portfolio": snapshot},
            }
        except Exception as exc:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("PORTFOLIO_QUERY_FAILED", str(exc), retryable=True)],
                }
            }

    async def gas_check(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("gas_request") or {}
        context = state.get("wallet_context") or {}
        chain = str(raw.get("chain") or context.get("chain") or "").upper()
        address = str(raw.get("address") or context.get("address") or "")
        to = raw.get("to")
        data = raw.get("data")
        adapter = runtime.chains.get(chain)
        if adapter is None:
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            "CHAIN_CAPABILITY_UNAVAILABLE",
                            f"Chain {chain} is unavailable.",
                            details={"chain": chain},
                        )
                    ],
                }
            }
        if not address:
            return {
                "response": {
                    "kind": "clarification",
                    "message": "请先连接钱包，或提供钱包地址。",
                    "missing_fields": ["wallet_address"],
                }
            }
        try:
            fee = await adapter.estimate_fee(to=to, data=data)
            native = await adapter.get_native_balance(address)
            balance_raw = int(native.amount_raw)
            fee_raw = int(fee.amount_raw)
            sufficient = balance_raw >= fee_raw
            shortfall_raw = max(fee_raw - balance_raw, 0)
            if sufficient:
                message = "当前原生币余额足够支付预计网络手续费。"
            else:
                message = "当前原生币余额不足以支付预计网络手续费。"
            warnings = []
            if context.get("chain") and str(context["chain"]).upper() != chain:
                warnings.append(f"当前钱包在 {context['chain']}，签名时需要切换到 {chain}。")
            snapshot = {
                "address": address,
                "chain": chain,
                "chain_id": context.get("chain_id"),
                "fee_estimate": _fee_dump(fee),
                "native_balance": _dump(native),
                "sufficient": sufficient,
                "shortfall_raw": str(shortfall_raw),
                "warnings": warnings,
                "message": message,
            }
            return {
                "gas_snapshot": snapshot,
                "response": {"kind": "gas_check", **snapshot},
            }
        except Exception as exc:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("GAS_CHECK_FAILED", str(exc), retryable=True)],
                }
            }

    async def asset_discovery(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("asset_query") or {}
        try:
            query = AssetQuery.model_validate(raw)
        except Exception as exc:
            return {
                "response": {
                    "kind": "clarification",
                    "message": "资产搜索条件无效。",
                    "errors": [_error("INVALID_ASSET_QUERY", str(exc))],
                }
            }
        if not runtime.providers:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("ASSET_PROVIDER_UNAVAILABLE", "没有可用的资产数据源。")],
                }
            }
        assets: list[Asset] = []
        provider_errors = []
        for provider_name, provider in runtime.providers.items():
            if not hasattr(provider, "list_assets"):
                continue
            try:
                values = await provider.list_assets(query)
                assets.extend(values)
            except Exception as exc:
                provider_errors.append(
                    _error(
                        "ASSET_DISCOVERY_FAILED",
                        str(exc),
                        retryable=True,
                        details={"provider": provider_name},
                    )
                )
        merged: dict[tuple[str, str, str], Asset] = {}
        for asset in assets:
            key = (
                str(asset.chain).upper(),
                str(asset.symbol).upper(),
                str(asset.address or "").lower(),
            )
            merged[key] = asset
        result_assets = [_dump(asset) for asset in merged.values()]
        snapshot: dict[str, Any] = {
            "query": query.model_dump(mode="json"),
            "assets": result_assets,
            "providers": list(runtime.providers),
            "provider_errors": provider_errors,
        }
        return {
            "asset_snapshot": snapshot,
            "response": {
                "kind": "asset_discovery",
                "assets": result_assets,
                "query": snapshot["query"],
                "provider_errors": provider_errors,
            },
        }

    async def price_query(state: dict[str, Any]) -> dict[str, Any]:
        """Resolve a token/native USD price through the injected provider."""
        if runtime.price_provider is None:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("PRICE_PROVIDER_UNAVAILABLE", "Price provider unavailable.")],
                }
            }
        raw = state.get("price_request") or state.get("request", {}).get("price_request")
        if raw is None:
            return {
                "response": {
                    "kind": "clarification",
                    "errors": [
                        _error("MISSING_PRICE_PARAMETERS", "Price parameters are required.")
                    ],
                }
            }
        try:
            from wallet_agent.domain.models import Asset

            asset = Asset.model_validate(raw)
            prices = await runtime.price_provider.get_prices([asset])
            return {"response": {"kind": "price_query", "prices": [_dump(item) for item in prices]}}
        except Exception as exc:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("PRICE_QUERY_FAILED", str(exc), retryable=True)],
                }
            }

    async def transaction_status(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("transaction_query") or state.get("request", {}).get("transaction_query")
        if not raw:
            return {
                "response": {
                    "kind": "clarification",
                    "message": "请提供交易所在链和交易哈希。",
                    "missing_fields": ["transaction_chain", "transaction_hash"],
                }
            }
        chain = str(raw.get("chain") or raw.get("transaction_chain") or "").upper()
        tx_hash = str(raw.get("tx_hash") or raw.get("transaction_hash") or raw.get("hash") or "")
        adapter = runtime.chains.get(chain)
        if adapter is None:
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            "CHAIN_CAPABILITY_UNAVAILABLE",
                            f"Chain {chain} is unavailable.",
                            details={"chain": chain},
                        )
                    ],
                }
            }
        try:
            status = await adapter.get_transaction_status(tx_hash)
            status_value = getattr(status, "value", str(status))
            messages = {
                "pending": "交易已提交，正在等待链上确认。",
                "confirmed": "交易已确认。",
                "failed": "交易执行失败或已回滚。",
                "dropped": "交易可能已被节点丢弃，请检查钱包或重新提交。",
                "unknown": "暂时无法确定交易状态。",
            }
            snapshot: dict[str, Any] = {
                "chain": chain,
                "tx_hash": tx_hash,
                "status": status_value,
                "message": messages.get(status_value, messages["unknown"]),
            }
            if hasattr(adapter, "get_transaction_receipt"):
                try:
                    receipt = await adapter.get_transaction_receipt(tx_hash)
                    if receipt is not None:
                        snapshot["receipt"] = _dump(receipt)
                except Exception:
                    pass
            return {
                "transaction_status_snapshot": snapshot,
                "response": {"kind": "transaction_status", **snapshot},
            }
        except Exception as exc:
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            "TRANSACTION_STATUS_FAILED",
                            str(exc),
                            retryable=True,
                            details={"chain": chain, "tx_hash": tx_hash},
                        )
                    ],
                }
            }

    async def prepare(state: dict[str, Any]) -> dict[str, Any]:
        selected = state.get("selected_quote")
        if selected is None:
            return {
                "response": {
                    "kind": "clarification",
                    "message": "A quote is required before preparation.",
                }
            }
        if state.get("pending_transaction"):
            return {}
        confirmation = state.get("user_confirmation")
        if confirmation is None:
            answer = interrupt({"kind": "confirmation_required", "quote": selected})
            if isinstance(answer, dict):
                confirmation = answer
            elif answer is True:
                confirmation = {"approved": True}
            else:
                confirmation = {"approved": False}
        if not confirmation.get("approved", False):
            return {
                "user_confirmation": confirmation,
                "response": {"kind": "cancelled", "message": "Swap cancelled."},
            }
        provider_name = str(selected.get("provider"))
        provider = runtime.providers.get(provider_name)
        if provider is None:
            return {
                "errors": [
                    _error("PROVIDER_UNAVAILABLE", f"Provider {provider_name} is unavailable.")
                ]
            }
        try:
            prepared = await provider.prepare(_quote(selected))
            preflight = None
            if isinstance(prepared, dict) and {"to", "data"}.issubset(prepared):
                prepared_transaction = UnsignedTransaction.model_validate(prepared)
            elif hasattr(prepared, "to") and hasattr(prepared, "data"):
                prepared_transaction = prepared
            else:
                prepared_transaction = None
            request = state.get("swap_request") or {}
            source_asset = request.get("source_asset") or selected.get("source_asset") or {}
            source_chain = str(source_asset.get("chain") or selected.get("chain") or "")
            adapter = runtime.chains.get(source_chain.upper())
            if prepared_transaction is not None and adapter is not None:
                preflight = await _transaction_preflight(
                    adapter=adapter,
                    chain=source_chain,
                    sender=str(request.get("sender_address") or ""),
                    recipient=str(request.get("recipient_address") or ""),
                    amount_raw=str(request.get("input_amount_raw") or "0"),
                    token=(
                        Asset.model_validate(source_asset) if source_asset.get("address") else None
                    ),
                    transaction=prepared_transaction,
                    wallet_context=state.get("wallet_context"),
                )
                if not preflight["ok"]:
                    return {
                        "preflight": preflight,
                        "user_confirmation": confirmation,
                        "response": {
                            "kind": "error",
                            "errors": [
                                _error(
                                    "TRANSACTION_PREFLIGHT_FAILED",
                                    "交易预检查未通过。",
                                    details={"preflight": preflight},
                                )
                            ],
                            "preflight": preflight,
                        },
                    }
            return {
                "user_confirmation": confirmation,
                "preflight": preflight,
                "pending_transaction": _dump(prepared),
                "response": {
                    "kind": "swap_prepare",
                    "transaction": _dump(prepared),
                    **({"preflight": preflight} if preflight is not None else {}),
                },
            }
        except Exception as exc:
            return {
                "errors": [
                    _error("PROVIDER_PREPARE_FAILED", str(exc), details={"provider": provider_name})
                ]
            }

    async def register_broadcast(state: dict[str, Any]) -> dict[str, Any]:
        tx_hash = state.get("broadcast_tx_hash")
        selected = state.get("selected_quote") or {}
        provider_name = str(selected.get("provider", ""))
        if not tx_hash or not provider_name:
            return {
                "errors": [
                    _error(
                        "MISSING_BROADCAST_HASH", "A chain-qualified transaction hash is required."
                    )
                ]
            }
        orders = state.get("provider_orders", {})
        if provider_name in orders:
            return {
                "provider_order_ids": {provider_name: orders[provider_name]["provider_order_id"]}
            }
        provider = runtime.providers.get(provider_name)
        if provider is None:
            return {
                "errors": [
                    _error("PROVIDER_UNAVAILABLE", f"Provider {provider_name} is unavailable.")
                ]
            }
        try:
            order = await provider.register_broadcast(
                selected.get("provider_reference", ""), tx_hash
            )
            value = _dump(order)
            return {
                "provider_order_ids": {provider_name: order.provider_order_id},
                "provider_orders": {provider_name: value},
            }
        except Exception as exc:
            return {
                "errors": [
                    _error(
                        "PROVIDER_REGISTER_FAILED", str(exc), details={"provider": provider_name}
                    )
                ]
            }

    async def status_poll(state: dict[str, Any]) -> dict[str, Any]:
        status_messages = {
            "pending": "兑换订单已提交，Provider 仍在处理中。",
            "completed": "兑换已完成，可以到目标链查看到账资产。",
            "failed": "兑换没有成功完成，请检查订单详情或稍后重试。",
            "refunded": "兑换已退款，请检查原资产是否已退回。",
            "timed_out": "Provider 暂时没有返回最终结果，建议稍后重新查询订单状态。",
            "unknown": "暂时拿不到兑换订单的最新状态，建议稍后重新查询。",
        }
        orders = state.get("provider_orders", {})
        if not orders:
            return {
                "response": {
                    "kind": "swap_status",
                    "status": "unknown",
                    "message": status_messages["unknown"],
                },
                "poll_attempts": state.get("poll_attempts", 0) + 1,
            }
        provider_name, raw_order = next(iter(orders.items()))
        provider = runtime.providers.get(provider_name)
        if provider is None:
            return {
                "errors": [
                    _error("PROVIDER_UNAVAILABLE", f"Provider {provider_name} is unavailable.")
                ]
            }
        try:
            status = await provider.get_status(ProviderOrder.model_validate(raw_order))
            dumped = _dump(status)
            attempts = state.get("poll_attempts", 0) + 1
            terminal = {
                "completed",
                "failed",
                "refunded",
                "timed_out",
                "kyc_required",
                "cancelled",
            }
            if dumped.get("status") not in terminal and attempts >= state.get(
                "max_poll_attempts", runtime.max_poll_attempts
            ):
                dumped = {**dumped, "status": "timed_out"}
            return {
                "status_snapshot": dumped,
                "poll_attempts": attempts,
                "response": {
                    "kind": "swap_status",
                    "status": dumped,
                    "message": status_messages.get(
                        dumped.get("status"), "兑换订单状态已更新，建议稍后重新查询。"
                    ),
                },
            }
        except Exception as exc:
            return {
                "errors": [
                    _error("PROVIDER_STATUS_FAILED", str(exc), details={"provider": provider_name})
                ],
                "poll_attempts": state.get("poll_attempts", 0) + 1,
            }

    async def response(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("response"):
            return {}
        if state.get("intent") == "clarification":
            response: dict[str, Any] = {
                "kind": "clarification",
                "errors": state.get("errors", []),
            }
            if not response["errors"] and not state.get("missing_fields"):
                response["message"] = (
                    "你好！我可以帮你查询余额、比较兑换报价、发起转账或兑换。"
                    "请先连接钱包，再告诉我你的需求。"
                )
            return {"response": response}
        if state.get("errors"):
            return {"response": {"kind": "error", "errors": state["errors"]}}
        return {"response": {"kind": state.get("intent", "clarification")}}

    return locals()
