"""Small graph nodes and runtime adapters."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from time import perf_counter
from typing import Annotated, Any

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool
from langgraph.config import get_stream_writer
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.prebuilt import InjectedState, ToolNode
from langgraph.types import interrupt

from wallet_agent.chains._common import receipt_success
from wallet_agent.domain.models import (
    AgentError,
    ApprovalTransaction,
    Asset,
    AssetQuery,
    DepositOrder,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    TransactionContext,
    TransferRequest,
    UnsignedTransaction,
)
from wallet_agent.domain.normalization import (
    canonical_chain,
    canonical_symbol,
    chain_id_for,
    mentioned_amount_with_unit,
    mentioned_fiat_value,
    swap_direction_hints,
    transaction_query_hints,
    unambiguous_amount_with_unit,
)
from wallet_agent.models import RouteDecision
from wallet_agent.observability import MetadataCache, RuntimeMetrics, wallet_event

from .dialogue import resolve_quote_reference, resolve_read_reference
from .facts import validate_response_draft
from .read_only import (
    READ_ONLY_MAX_PARALLEL_TOOLS,
    READ_ONLY_MAX_ROUNDS,
    READ_ONLY_TOOL_NAMES,
    READ_ONLY_TOOL_SCHEMAS,
    ReadOnlyToolCall,
    read_only_decision,
    read_only_tool_name_for_intent,
    read_tool_signature,
    run_read_only_round,
)
from .state import DialogueState
from .tasks import (
    hydrate_active_task,
    merge_task_patch,
    new_active_task,
    project_legacy_draft,
)

_RESPONSE_DATA_KEYS = {
    "swap_quote": {
        "intent",
        "source_chain",
        "destination_chain",
        "source_symbol",
        "destination_symbol",
        "source_token_address",
        "destination_token_address",
        "input_amount",
        "output_amount",
        "amount_mode",
        "target_value_amount",
        "target_value_currency",
        "slippage_bps",
    },
    "transfer": {"intent", "chain", "symbol", "token_address", "decimals", "amount", "recipient"},
}


def _normalize_price_asset(
    raw: Mapping[str, Any],
    args: Mapping[str, Any],
    wallet_context: Mapping[str, Any] | None = None,
) -> tuple[Asset | None, dict[str, Any] | None]:
    """Resolve trusted metadata required by the price provider."""
    values = {**dict(raw), **dict(args)}
    symbol = canonical_symbol(str(values.get("symbol") or ""))
    if not symbol:
        return None, {"code": "PRICE_ASSET_UNRESOLVED", "error": "请指定要查询的资产。"}
    values["symbol"] = symbol
    context = wallet_context or {}
    chain_value = values.get("chain") or context.get("chain")
    chain = canonical_chain(str(chain_value)) if chain_value else ""
    token_address = values.pop("token_address", None)
    if values.get("address") is None and token_address:
        values["address"] = token_address
    explicit_address = values.get("address")
    if not chain:
        inferred = _unique_native_chain(symbol, explicit_address)
        if inferred is None and symbol == "ETH":
            inferred = "ETH"
        if inferred:
            chain = inferred
        else:
            return None, {
                "code": "PRICE_CHAIN_REQUIRED",
                "error": f"请问你想查询哪个网络上的 {symbol}？例如 Ethereum、BSC 或 Base。",
                "symbol": symbol,
            }
    values["chain"] = chain
    if (
        values.get("decimals") is None
        and _native_symbol(chain, None) == symbol
        and not _explicit_token_address(explicit_address)
    ):
        values["decimals"] = _NATIVE_ASSET_DECIMALS.get(chain)
    if values.get("chain_id") is None:
        values["chain_id"] = chain_id_for(chain)
    if values.get("decimals") is None:
        return None, {
            "code": "PRICE_ASSET_UNRESOLVED",
            "error": f"正在通过资产目录确认 {symbol} 在 {chain} 上的信息。",
            "symbol": symbol,
            "chain": chain,
        }
    try:
        return Asset.model_validate(values), None
    except Exception:
        return None, {
            "code": "PRICE_ASSET_UNRESOLVED",
            "error": f"暂时无法确认 {symbol} 的资产信息，请确认资产和网络是否受支持。",
            "symbol": symbol,
            "chain": chain,
        }


async def _resolve_asset_metadata(
    values: Mapping[str, Any],
    providers: Mapping[str, Any],
    wallet_context: Mapping[str, Any] | None = None,
) -> tuple[Asset | None, list[dict[str, Any]]]:
    """Resolve asset identifiers using chain-specific wallet/provider catalogs."""
    merged = dict(values)
    context = wallet_context or {}
    symbol = canonical_symbol(str(merged.get("symbol") or ""))
    chain_value = merged.get("chain") or context.get("chain")
    if not symbol or not chain_value:
        return None, []
    chain = canonical_chain(str(chain_value))
    explicit_address = merged.get("address") or merged.get("token_address")
    matches: list[Asset] = []
    for provider in providers.values():
        if not hasattr(provider, "list_assets"):
            continue
        try:
            matches.extend(await provider.list_assets(AssetQuery(chain=chain, search=symbol)))
        except Exception:
            continue
    unique = {
        (str(item.address or "").lower(), int(item.decimals)): item
        for item in matches
        if canonical_chain(str(item.chain)) == chain
        and canonical_symbol(str(item.symbol)) == symbol
        and (
            not explicit_address
            or str(item.address or "").lower() == str(explicit_address).lower()
        )
    }
    if len(unique) == 1:
        return next(iter(unique.values())), []
    return None, [
        {
            "chain": item.chain,
            "symbol": item.symbol,
            "address": item.address,
            "decimals": item.decimals,
        }
        for item in unique.values()
    ]


def _suggestion_data_for_model(value: Any) -> dict[str, Any] | None:
    """Expose only user-confirmable slot fields from UI suggestion metadata."""
    if not isinstance(value, Mapping):
        return None
    allowed = _RESPONSE_DATA_KEYS["swap_quote"] | _RESPONSE_DATA_KEYS["transfer"]
    clean = {str(key): _dump(item) for key, item in value.items() if str(key) in allowed}
    return clean or None


def _message_language(message: str) -> str | None:
    if re.search(r"[\u3400-\u9fff]", message):
        return "zh"
    # Numeric amounts and bare wallet addresses do not carry a language signal.
    text = re.sub(r"0x[0-9a-fA-F]+", "", message)
    return "en" if re.search(r"[A-Za-z]{2,}", text) else None


def _response_language(message: str, history: Any = None) -> str:
    current = _message_language(message)
    if current:
        return current
    for item in reversed(history or []):
        if not isinstance(item, Mapping):
            continue
        language = _message_language(str(item.get("content") or ""))
        if language:
            return language
    return "en"


def _response_fallback(intent: str, missing: list[str], language: str) -> str:
    if language == "zh":
        if intent == "unsupported":
            return "抱歉，我目前只能处理钱包余额、资产、价格、转账和兑换相关请求。"
        if intent == "clarification" and not missing:
            return "你好！我可以帮你查询余额、比较兑换报价、发起转账或兑换。请告诉我具体需求。"
        if intent == "transfer":
            phrases = {
                "transfer_recipient": "收款地址",
                "transfer_amount": "转账金额",
                "transfer_chain": "转账网络",
                "transfer_symbol": "转账资产",
            }
            labels = [phrases[item] for item in missing if item in phrases]
            return f"请补充{'、'.join(labels) or '这笔转账所需的信息'}。"
        if intent == "swap_quote":
            if missing == ["input_amount"]:
                return "你要使用的 USDC 数量是多少？"
            return "请告诉我想换出的资产、目标资产和数量。"
        return "请再告诉我一些具体信息。"
    if intent == "unsupported":
        return (
            "Sorry, I can currently only help with wallet balances, assets, prices, "
            "transfers, and swaps."
        )
    if intent == "transfer":
        phrases = {
            "transfer_recipient": "the recipient address",
            "transfer_amount": "the transfer amount",
            "transfer_chain": "the network",
            "transfer_symbol": "the asset",
        }
        labels = [phrases[item] for item in missing if item in phrases]
        return f"Please provide {', '.join(labels) or 'the missing transfer details'}."
    if intent == "clarification" and not missing:
        return (
            "Hi! I can help check balances, compare swap quotes, or prepare transfers and swaps. "
            "What would you like to do?"
        )
    if intent == "swap_quote":
        return "What asset would you like to swap, and what would you like to receive?"
    return "Please provide a little more detail about what you need."


def _display_decimal(value: str, *, places: int) -> str:
    try:
        decimal_value = Decimal(value)
        quantum = Decimal(1).scaleb(-places)
        rendered = format(decimal_value.quantize(quantum, rounding=ROUND_DOWN), "f")
        return rendered.rstrip("0").rstrip(".") or "0"
    except (ArithmeticError, ValueError):
        return value


def _swap_clarification_message(
    state: Mapping[str, Any], missing: list[str], language: str
) -> str:
    """Ask only for unresolved swap slots, using already confirmed task facts."""
    draft = state.get("swap_draft") or {}
    if not isinstance(draft, Mapping):
        draft = {}
    source = str(draft.get("source_symbol") or "").strip()
    destination = str(draft.get("destination_symbol") or "").strip()
    request = state.get("request") or {}
    message = str(request.get("message") or "") if isinstance(request, Mapping) else ""
    fiat_hint = mentioned_fiat_value(message)
    fiat_value = str(draft.get("target_value_amount") or (fiat_hint or (None, None))[0] or "")
    output_amount = str(draft.get("output_amount") or "")
    price_usd = str(draft.get("target_value_price_usd") or "")
    output_label = _display_decimal(output_amount, places=8) if output_amount else ""
    price_label = _display_decimal(price_usd, places=2) if price_usd else ""

    if language == "zh":
        known = f"我已识别出用 {source} 兑换 {destination}。" if source and destination else ""
        if fiat_value and output_amount and destination:
            known += (
                f"按当前 {destination} 价格{f'（约 {price_label} USD）' if price_label else ''}，"
                f"价值 {fiat_value}u 约为 {output_label} {destination}。"
            )
        questions: list[str] = []
        if "source_chain" in missing:
            questions.append(f"{source or '来源资产'} 使用哪个网络")
        if "destination_chain" in missing:
            questions.append(f"{destination or '目标资产'} 使用哪个网络")
        if "source_symbol" in missing:
            questions.append("想换出哪种资产")
        if "destination_symbol" in missing:
            questions.append("想换成哪种资产")
        if "input_amount" in missing:
            if fiat_value:
                questions.append(
                    f"你提到的“价值 {fiat_value}u”是美元价值；当前报价还需要准备投入的 "
                    f"{source or '来源资产'} 数量"
                )
            else:
                questions.append(f"准备投入多少 {source or '来源资产'}")
        if "output_amount" in missing:
            questions.append(f"希望收到多少 {destination or '目标资产'}")
        if "sender_address" in missing or "recipient_address" in missing:
            questions.append("请先连接钱包，以便填写发送和接收地址")
        return f"{known}还需要确认：{'；'.join(questions) or '兑换参数'}。"

    known = (
        f"I understood that you want to swap {source} for {destination}. "
        if source and destination
        else ""
    )
    if fiat_value and output_amount and destination:
        known += (
            f"At the current {destination} price"
            f"{f' (about {price_label} USD)' if price_label else ''}, "
            f"{fiat_value} USD is about {output_label} {destination}. "
        )
    questions = []
    if "source_chain" in missing:
        questions.append(f"the network for {source or 'the source asset'}")
    if "destination_chain" in missing:
        questions.append(f"the network for {destination or 'the destination asset'}")
    if "source_symbol" in missing:
        questions.append("the asset to spend")
    if "destination_symbol" in missing:
        questions.append("the asset to receive")
    if "input_amount" in missing:
        if fiat_value:
            questions.append(
                f"the {source or 'source asset'} amount to spend; "
                f"{fiat_value} USD is a fiat-value target"
            )
        else:
            questions.append(f"the {source or 'source asset'} amount to spend")
    if "output_amount" in missing:
        questions.append(f"the desired {destination or 'destination asset'} amount")
    if "sender_address" in missing or "recipient_address" in missing:
        questions.append("connect the wallet for sender and recipient addresses")
    return f"{known}Please confirm {', '.join(questions) or 'the missing swap details'}."


def _sanitize_response_suggestions(
    suggestions: Any,
    *,
    intent: str,
    facts: Mapping[str, Any],
) -> list[dict[str, Any]]:
    allowed = _RESPONSE_DATA_KEYS.get(intent, set())
    chains = {str(value).upper() for value in facts.get("supported_chains", [])}
    asset_keys = {
        (str(item.get("chain", "")).upper(), str(item.get("symbol", "")).upper())
        for item in facts.get("supported_assets", [])
        if isinstance(item, Mapping)
    }
    balance_keys = {
        (str(item.get("chain", "")).upper(), str(item.get("symbol", "")).upper())
        for item in facts.get("balances", [])
        if isinstance(item, Mapping)
    }
    output: list[dict[str, Any]] = []
    for suggestion in suggestions or []:
        item = _dump(suggestion)
        if not isinstance(item, Mapping):
            continue
        data = item.get("data")
        if not isinstance(data, Mapping):
            continue
        clean = {str(key): value for key, value in data.items() if key in allowed}
        clean_intent = "swap_quote" if intent == "swap_quote" else intent
        if clean.get("intent") not in (None, clean_intent):
            continue
        clean["intent"] = clean_intent
        invalid = False
        for key in ("source_chain", "destination_chain", "chain"):
            if clean.get(key) is not None and chains and str(clean[key]).upper() not in chains:
                invalid = True
        if invalid:
            continue
        for side in ("source", "destination"):
            chain_key = f"{side}_chain"
            symbol_key = f"{side}_symbol"
            if clean.get(symbol_key) and clean.get(chain_key) and asset_keys:
                asset_key = (str(clean[chain_key]).upper(), str(clean[symbol_key]).upper())
                if asset_key not in asset_keys:
                    invalid = True
                if side == "source" and balance_keys and asset_key not in balance_keys:
                    invalid = True
        if invalid:
            continue
        output.append({**dict(item), "data": clean})
    return output



@dataclass(frozen=True)
class GraphRuntime:
    model: Any
    providers: dict[str, Any]
    chains: dict[str, Any]
    wallet_provider: Any | None = None
    explorer_provider: Any | None = None
    execution_observer: Any | None = None
    price_provider: Any | None = None
    max_poll_attempts: int = 3
    confirmation_ttl_seconds: int = 900
    provider_timeout_seconds: float = 15
    max_react_steps: int = 8
    react_tool_timeout_seconds: float = 20
    require_react: bool = False
    metadata_cache: MetadataCache | None = None
    metrics: RuntimeMetrics | None = None


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
    "market_details",
    "price_history",
}

_TRANSFER_CANONICAL_KEYS = {
    "transfer_chain": "chain",
    "transfer_chain_id": "chain_id",
    "transfer_symbol": "symbol",
    "transfer_token_address": "token_address",
    "transfer_decimals": "decimals",
    "transfer_amount": "amount",
    "transfer_amount_raw": "amount_raw",
    "transfer_sender": "sender",
    "transfer_recipient": "recipient",
}
_USER_SWAP_FIELDS = frozenset(
    {
        "source_chain",
        "destination_chain",
        "source_symbol",
        "destination_symbol",
        "input_amount",
        "output_amount",
        "sender_address",
        "recipient_address",
        "slippage_bps",
    }
)

# These are internal provider-resolved fields. Never expose them as a user
# clarification: users provide asset names/networks, while providers supply
# contract addresses and decimals.
_INTERNAL_ASSET_FIELDS = frozenset(
    {
        "source_token_address",
        "destination_token_address",
        "source_decimals",
        "destination_decimals",
    }
)


def _emit_progress(
    stage: str,
    status: str,
    *,
    started: float | None = None,
    message: str,
    **details: Any,
) -> None:
    """Emit a safe, checkpoint-free progress event when running in LangGraph."""
    elapsed_ms = 0.0 if started is None else (perf_counter() - started) * 1000
    payload = {
        "stage": stage,
        "status": status,
        "elapsed_ms": round(elapsed_ms, 2),
        "message": message,
        **details,
    }
    try:
        get_stream_writer()(payload)
    except RuntimeError:
        # Nodes are also called directly in unit tests and utility code, where
        # no LangGraph streaming context exists.
        return


def _task_patch(kind: str, value: Any) -> dict[str, Any]:
    dumped = _dump(value)
    if not isinstance(dumped, dict):
        return {}
    if kind == "transfer":
        result: dict[str, Any] = {}
        for key, item in dumped.items():
            canonical = _TRANSFER_CANONICAL_KEYS.get(key, key)
            if canonical in _TRANSFER_CANONICAL_KEYS.values() and item is not None:
                result[canonical] = item
        return result
    return {key: dumped[key] for key in _SWAP_DRAFT_KEYS if dumped.get(key) is not None}


def _resolved_task(task: dict[str, Any], slots: dict[str, Any]) -> dict[str, Any]:
    updated = {**task, "slots": dict(slots)}
    sources = dict(task.get("slot_sources") or {})
    for key in slots:
        if key not in sources:
            sources[key] = "resolver"
    updated["slot_sources"] = sources
    return updated


def _task_progress(
    task: dict[str, Any],
    *,
    slots: dict[str, Any],
    status: str,
    stage: str,
    missing_fields: list[str],
) -> dict[str, Any]:
    return {
        **_resolved_task(task, slots),
        "status": status,
        "stage": stage,
        "missing_fields": list(missing_fields),
        "updated_by": "system",
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
        for key in (
            "chain",
            "chain_id",
            "amount_raw",
            "gas_limit",
            "max_fee_per_gas",
            "max_priority_fee_per_gas",
            "expires_at",
        )
        if hasattr(value, key)
    }
    if hasattr(value, "amount"):
        result["amount"] = str(value.amount)
    if hasattr(value, "asset"):
        asset = _dump(value.asset)
        result["asset"] = asset if isinstance(asset, dict) else str(asset)
    return result


def _apply_fee_estimate(transaction: Any, fee: Mapping[str, Any] | None) -> Any:
    """Copy the exact preflight gas fields onto the wallet transaction payload."""
    if not isinstance(transaction, UnsignedTransaction) or not isinstance(fee, Mapping):
        return transaction
    updates = {
        key: fee[key]
        for key in ("gas_limit", "max_fee_per_gas", "max_priority_fee_per_gas")
        if fee.get(key) is not None
    }
    return transaction.model_copy(update=updates) if updates else transaction


def _deposit_order_to_transaction(
    order: Any,
    *,
    adapter: Any,
    sender: str,
) -> UnsignedTransaction | Any:
    """Turn a provider deposit order into the source-chain wallet transaction."""
    if not isinstance(order, DepositOrder):
        return order
    if not sender:
        raise ValueError("provider deposit transaction requires the connected wallet address")
    if order.source_asset.address:
        builder = getattr(adapter, "build_erc20_transfer", None)
        if builder is None:
            raise ValueError(
                f"{order.source_asset.chain} adapter cannot build an ERC-20 deposit transaction"
            )
        transaction = builder(
            token=order.source_asset,
            from_address=sender,
            to_address=order.deposit_address,
            amount_raw=order.input_amount_raw,
        )
    else:
        builder = getattr(adapter, "build_native_transfer", None)
        if builder is None:
            raise ValueError(
                f"{order.source_asset.chain} adapter cannot build a native deposit transaction"
            )
        transaction = builder(
            from_address=sender,
            to_address=order.deposit_address,
            amount_raw=order.input_amount_raw,
        )
    return transaction.model_copy(
        update={
            "provider": order.provider,
            "provider_reference": order.provider_reference,
            "expires_at": order.expires_at,
            "display": {
                "action": "provider_deposit",
                "provider": order.provider,
                "deposit_address": order.deposit_address,
                "input_amount": str(order.input_amount),
                "input_amount_raw": order.input_amount_raw,
                "provider_order_id": order.provider_order_id,
            },
        }
    )


def _quote(value: Any) -> NormalizedQuote:
    return value if isinstance(value, NormalizedQuote) else NormalizedQuote.model_validate(value)


def _request(value: Any) -> SwapQuoteRequest:
    payload = _dump(value)
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.pop("output_amount", None)
        payload.pop("amount_mode", None)
    return SwapQuoteRequest.model_validate(payload)


def _request_json(value: Any) -> dict[str, Any]:
    """Return the canonical JSON-safe representation used by graph/tool state."""
    return _request(value).model_dump(mode="json")


def _same_swap_asset(request: SwapQuoteRequest) -> bool:
    """Return whether both sides identify the same token on the same chain."""
    source = request.source_asset
    destination = request.destination_asset
    source_address = str(source.address or "").strip().lower()
    destination_address = str(destination.address or "").strip().lower()
    same_chain = canonical_chain(source.chain) == canonical_chain(destination.chain)
    if not same_chain:
        return False
    if source_address or destination_address:
        return bool(
            source_address and destination_address and source_address == destination_address
        )
    return bool(
        _native_swap_asset(source.chain, source.symbol)
        and _native_swap_asset(destination.chain, destination.symbol)
        and canonical_symbol(source.symbol) == canonical_symbol(destination.symbol)
    )


def _mapping(value: Any) -> dict[str, Any]:
    """Coerce legacy structured input to a plain JSON-safe mapping."""
    dumped = _dump(value)
    return dumped if isinstance(dumped, dict) else {}


def _transaction_native_amount(value: Any) -> str:
    """Normalize an unsigned transaction value for OKX preflight APIs."""
    text = str(value if value not in (None, "") else "0").strip()
    if text.lower().startswith("0x"):
        return str(int(text, 16))
    if not text.isdigit():
        raise ValueError("transaction value must be a decimal or 0x-prefixed integer")
    return text


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


def latest_public_result(history: Any) -> dict[str, Any] | None:
    """Return the latest immutable user-visible response card from history."""
    for item in reversed(history or []):
        if not isinstance(item, Mapping) or item.get("role") != "assistant":
            continue
        artifacts = item.get("artifacts")
        if not isinstance(artifacts, Mapping):
            continue
        response = artifacts.get("response")
        if not isinstance(response, Mapping):
            continue
        return dict(response)
    return None


def _quote_is_usable(quote: Mapping[str, Any]) -> bool:
    """Reject provider placeholders that are technically successful but unusable."""
    try:
        return (
            Decimal(str(quote.get("input_amount"))) > 0
            and Decimal(str(quote.get("expected_output"))) > 0
            and bool(quote.get("provider_reference"))
        )
    except (ArithmeticError, TypeError, ValueError):
        return False


def _best_react_quote_result(
    tool_payloads: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any] | None:
    """Select the latest valid request result without allowing zero-output probes to win."""
    candidates: list[dict[str, Any]] = []
    for name, payload in tool_payloads:
        if name != "get_swap_quote" or not payload.get("ok"):
            continue
        quotes = [
            dict(item)
            for item in (payload.get("quotes") or [])
            if isinstance(item, Mapping) and _quote_is_usable(item)
        ]
        if quotes:
            candidates.append({**payload, "quotes": quotes})
    return candidates[-1] if candidates else None


def _valid_react_quotes(messages: list[Any]) -> list[dict[str, Any]]:
    """Extract valid quote observations from the current ReAct transcript."""
    result: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, ToolMessage) or str(message.name or "") != "get_swap_quote":
            continue
        try:
            payload = json.loads(str(message.content))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, Mapping) or not payload.get("ok"):
            continue
        result.extend(
            dict(item)
            for item in (payload.get("quotes") or [])
            if isinstance(item, Mapping) and _quote_is_usable(item)
        )
    return result


def _model_conversation_history(history: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    """Keep model context bounded while the full history remains checkpointed for the UI."""
    items = [item for item in (history or []) if isinstance(item, Mapping)]
    return [dict(item) for item in items[-limit:]]


def _latest_read_response(history: Any) -> dict[str, Any] | None:
    """Return the latest persisted wallet or portfolio result."""
    response = latest_public_result(history)
    if response and str(response.get("kind") or "") in {"wallet_query", "portfolio_query"}:
        return response
    return None


def _looks_like_asset_value_query(message: str) -> bool:
    """Recognize an explicit request to value the user's holding.

    This is intentionally narrow: a balance question is not an estimate request,
    while phrases such as ``我的 BNB 价值多少 U`` must not depend on the
    classifier choosing the right intent.
    """
    text = str(message or "").strip().lower()
    if not text:
        return False
    if re.search(r"(余额|持有量|有多少|balance)\s*(是多少|多少)?$", text):
        return False
    has_holding_reference = bool(
        re.search(r"(我的|我有|持有|账户里|钱包里|my\s+|my wallet)", text)
    )
    has_value_language = bool(
        re.search(r"(价值|worth|value|估值|值多少钱|多少\s*(?:u|usd|美元|刀))", text)
    )
    return has_holding_reference and has_value_language


def _latest_preflight_failure(history: Any) -> dict[str, Any] | None:
    """Find the most recent preflight failure persisted in assistant artifacts."""
    for item in reversed(history or []):
        if not isinstance(item, Mapping) or item.get("role") != "assistant":
            continue
        artifacts = item.get("artifacts")
        response = artifacts.get("response") if isinstance(artifacts, Mapping) else None
        if not isinstance(response, Mapping):
            continue
        errors = response.get("errors") or []
        for error in errors:
            if (
                not isinstance(error, Mapping)
                or error.get("code") != "TRANSACTION_PREFLIGHT_FAILED"
            ):
                continue
            details = error.get("details")
            preflight = details.get("preflight") if isinstance(details, Mapping) else None
            if isinstance(preflight, Mapping):
                return dict(preflight)
            if isinstance(response.get("preflight"), Mapping):
                return dict(response["preflight"])
            return {"chain": response.get("chain"), "checks": []}
    return None


def _looks_like_preflight_balance_followup(message: str, history: Any) -> bool:
    text = str(message or "").strip().lower()
    if not text or not _latest_preflight_failure(history):
        return False
    # A stale preflight failure is context, not a reason to hijack a new
    # business request. Require explicit confirmation language; phrases such
    # as "我的全部余额" and "所有资产有多少" must reach the model router.
    asks_about_balance = bool(
        re.search(r"(余额|余额不足|资金|不够|insufficient|balance|funds)", text)
    )
    asks_for_confirmation = bool(
        re.search(
            r"(^|\s)(是不是|是否|对不对|right|correct|yes)(\s|$)|"
            r"(余额|资金|balance|funds).*(吗|么|对不对|是不是|是否)($|\s)",
            text,
        )
    )
    return asks_about_balance and asks_for_confirmation


def _preflight_balance_response(preflight: Mapping[str, Any], language: str) -> dict[str, Any]:
    checks = preflight.get("checks") or []
    failed = next(
        (
            item
            for item in checks
            if isinstance(item, Mapping)
            and item.get("code") in {"INSUFFICIENT_BALANCE", "INSUFFICIENT_GAS"}
        ),
        None,
    )
    chain = str(preflight.get("chain") or "当前网络")
    if language == "zh":
        if failed:
            message = f"是的，这笔交易在 {chain} 的预检查显示钱包余额不足。"
            detail = str(failed.get("message") or "")
            if detail:
                message += f"{detail}"
            message += "这还不包含后续可能产生的网络手续费。"
        else:
            message = f"这笔交易在 {chain} 的预检查没有通过，原因与钱包余额或手续费有关。"
    else:
        message = (
            f"Yes. The preflight check on {chain} indicates that the wallet "
            "balance is insufficient."
            if failed
            else (
                f"The preflight check on {chain} did not pass; the issue is "
                "related to balance or fees."
            )
        )
    return {
        "kind": "clarification",
        "message": message,
        "preflight": dict(preflight),
        "errors": [
            _error(
                "INSUFFICIENT_BALANCE" if failed else "TRANSACTION_PREFLIGHT_FAILED",
                message,
                details={"preflight": dict(preflight)},
            )
        ],
    }


def _asset_value_response(
    facts: Mapping[str, Any],
    language: str,
    *,
    requested_symbol: str | None = None,
    requested_chain: str | None = None,
) -> dict[str, Any] | None:
    """Deterministically combine verified balance and price facts."""
    wallet_result = facts.get("wallet_balance")
    price_result = facts.get("price_lookup")
    wallet_ok = isinstance(wallet_result, Mapping) and wallet_result.get("ok")
    price_ok = isinstance(price_result, Mapping) and price_result.get("ok")
    if not wallet_ok and not price_ok:
        return None

    wallet = wallet_result.get("wallet") if wallet_ok else {}
    balances = _wallet_balance_items(wallet if isinstance(wallet, Mapping) else {})
    prices = price_result.get("prices") if price_ok else []
    prices = prices if isinstance(prices, list) else []
    selected_balance = balances[0] if len(balances) == 1 else None
    selected_price = prices[0] if len(prices) == 1 else None
    if selected_balance is None and requested_symbol:
        selected_balance = next(
            (
                item
                for item in balances
                if canonical_symbol(str((item.get("asset") or {}).get("symbol") or ""))
                == canonical_symbol(requested_symbol)
                and (
                    not requested_chain
                    or canonical_chain(str((item.get("asset") or {}).get("chain") or ""))
                    == canonical_chain(requested_chain)
                )
            ),
            None,
        )
    if selected_balance is None and selected_price is not None:
        price_asset = selected_price.get("asset") or {}
        selected_balance = next(
            (
                item
                for item in balances
                if canonical_symbol(str((item.get("asset") or {}).get("symbol") or ""))
                == canonical_symbol(str(price_asset.get("symbol") or ""))
                and canonical_chain(str((item.get("asset") or {}).get("chain") or ""))
                == canonical_chain(str(price_asset.get("chain") or ""))
            ),
            None,
        )
    if selected_price is None and selected_balance is not None:
        balance_asset = selected_balance.get("asset") or {}
        selected_price = next(
            (
                item
                for item in prices
                if canonical_symbol(str((item.get("asset") or {}).get("symbol") or ""))
                == canonical_symbol(str(balance_asset.get("symbol") or ""))
            ),
            None,
        )
    if selected_balance is not None and selected_price is not None:
        balance_asset = selected_balance.get("asset") or {}
        price_asset = selected_price.get("asset") or {}
        same_asset = (
            canonical_symbol(str(balance_asset.get("symbol") or ""))
            == canonical_symbol(str(price_asset.get("symbol") or ""))
            and canonical_chain(str(balance_asset.get("chain") or ""))
            == canonical_chain(str(price_asset.get("chain") or ""))
        )
        if not same_asset:
            # Never multiply a balance by a price for a different asset.
            selected_price = None
    if selected_balance is None and balances:
        selected_balance = balances[0]
    if selected_price is None and prices:
        selected_price = prices[0]

    balance_asset = (selected_balance or {}).get("asset") or {}
    price_asset = (selected_price or {}).get("asset") or {}
    asset = dict(price_asset or balance_asset)
    amount = (selected_balance or {}).get("amount")
    usd_price = (selected_price or {}).get("usd_price")
    response: dict[str, Any] = {
        "kind": "asset_value",
        "asset": asset,
        "balance": selected_balance,
        "price": selected_price,
    }
    if amount is not None and usd_price is not None:
        try:
            response["usd_value"] = str(Decimal(str(amount)) * Decimal(str(usd_price)))
            response["message"] = (
                f"你的 {asset.get('symbol', '资产')} 余额是 {amount}，"
                f"按当前价格 ${usd_price} 计算，"
                f"价值约为 ${response['usd_value']}。"
                if language == "zh"
                else (
                    f"Your {asset.get('symbol', 'asset')} balance is {amount}; "
                    f"at ${usd_price}, it is worth about ${response['usd_value']}."
                )
            )
        except (ArithmeticError, TypeError, ValueError):
            pass
    if "usd_value" not in response:
        failed = price_result if not price_ok else wallet_result
        response["error"] = failed
        response["message"] = (
            f"我查到你的 {asset.get('symbol', '资产')} 余额，但暂时无法计算美元价值。"
            if language == "zh"
            else (
                f"I found your {asset.get('symbol', 'asset')} balance, but "
                "cannot calculate its USD value right now."
            )
        )
    return response


def _dialogue_referent(response: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not response:
        return None
    kind = str(response.get("kind") or "")
    if kind == "price_query":
        prices = response.get("prices") or []
        if len(prices) != 1 or not isinstance(prices[0], Mapping):
            return None
        price = prices[0]
        asset = price.get("asset") or {}
        if not isinstance(asset, Mapping) or not asset.get("symbol"):
            return None
        facts = {
            "chain": asset.get("chain"),
            "symbol": asset.get("symbol"),
            "usd_price": price.get("usd_price"),
        }
        return {
            "kind": "asset",
            "id": f"{facts['chain']}:{facts['symbol']}",
            "facts": {key: value for key, value in facts.items() if value is not None},
        }
    if kind == "asset_value":
        asset = response.get("asset") or {}
        if not isinstance(asset, Mapping) or not asset.get("symbol"):
            return None
        facts = {
            "chain": asset.get("chain"),
            "symbol": asset.get("symbol"),
            "usd_value": response.get("usd_value"),
        }
        return {
            "kind": "asset",
            "id": f"{facts['chain']}:{facts['symbol']}",
            "facts": {key: value for key, value in facts.items() if value is not None},
        }
    if kind == "transaction_status":
        chain = response.get("chain")
        tx_hash = response.get("tx_hash")
        if not chain or not tx_hash:
            return None
        facts = {
            "chain": chain,
            "tx_hash": tx_hash,
            "status": response.get("status"),
        }
        return {
            "kind": "transaction",
            "id": f"{chain}:{tx_hash}",
            "facts": {key: value for key, value in facts.items() if value is not None},
        }
    if kind == "swap_quote":
        quotes = response.get("quotes") or []
        if not quotes:
            return None
        return {
            "kind": "quote",
            "id": "quote-set",
            "facts": {"quotes": quotes},
        }
    if kind in {"wallet_query", "portfolio_query"}:
        return {"kind": kind, "id": kind, "facts": dict(response)}
    return None


def dialogue_snapshot(state: Mapping[str, Any]) -> DialogueState:
    """Project one compact, JSON-safe dialogue context from graph state."""
    request = state.get("request") or {}
    message = str(request.get("message") or state.get("message") or "")
    result = latest_public_result(state.get("conversation_history"))
    kind = str(result.get("kind") or "") if result else ""
    topic_by_kind = {
        "wallet_query": "wallet",
        "portfolio_query": "portfolio",
        "price_query": "price",
        "asset_value": "price",
        "gas_check": "gas",
        "transaction_status": "transaction",
        "swap_status": "swap_status",
        "swap_quote": "swap",
        "transfer_prepare": "transfer",
        "swap_prepare": "swap",
    }
    active_topic = topic_by_kind.get(kind, "")
    goal = (
        "read"
        if active_topic in {"wallet", "portfolio", "price", "gas", "transaction"}
        else "unknown"
    )
    if active_topic in {"transfer", "swap"}:
        goal = active_topic
    elif active_topic == "swap_status":
        goal = "status"
    if not active_topic:
        lowered = message.lower()
        if any(token in lowered for token in ("转账", "transfer", "send")):
            goal, active_topic = "transfer", "transfer"
        elif any(token in lowered for token in ("兑换", "换", "swap")):
            goal, active_topic = "swap", "swap"
    return DialogueState(
        goal=goal,
        active_topic=active_topic or "unknown",
        referent=_dialogue_referent(result),
        language=_response_language(message, state.get("conversation_history")),
        last_result_kind=kind or None,
    )


def _history_followup_intent(message: str, history: Any) -> str | None:
    """Recognize short follow-ups that refer to the last balance/portfolio result."""
    previous = _latest_read_response(history)
    if previous is None:
        return None
    text = str(message or "").strip().lower()
    if not text:
        return None
    list_followup = re.search(
        r"(哪\s*(?:\d+|一|几)\s*项|哪些资产|哪些币|"
        r"分别(是什么|有多少)?|列(出来|一下|出)|list|which assets|what assets)",
        text,
    )
    asset_followup = re.search(
        r"(有多少|余额|多少|哪个.*(多|高|值)|价值|worth|how much|which.*(largest|highest))",
        text,
    )
    if not (list_followup or asset_followup):
        return None
    return str(previous.get("kind"))


def _wallet_balance_items(wallet: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = []
    native = wallet.get("native_balance")
    if isinstance(native, Mapping) and native.get("asset"):
        items.append(native)
    for item in wallet.get("token_balances") or []:
        if isinstance(item, Mapping) and item.get("asset"):
            items.append(item)
    return items


def _wallet_summary_message(wallet: Mapping[str, Any], language: str) -> str:
    items = _wallet_balance_items(wallet)
    if not items:
        return (
            "当前钱包没有查到可用余额。"
            if language == "zh"
            else "I couldn't find any available balance in this wallet."
        )
    lines = []
    for item in items:
        asset = item.get("asset") or {}
        symbol = str(asset.get("symbol") or "未知资产")
        amount = str(item.get("amount") or "0")
        lines.append(f"- {symbol}：{amount}" if language == "zh" else f"- {symbol}: {amount}")
    chain = str(wallet.get("chain") or "当前网络")
    if language == "zh":
        return f"{chain} 钱包目前有 {len(items)} 项资产：\n" + "\n".join(lines)
    return f"This wallet has {len(items)} assets on {chain}:\n" + "\n".join(lines)


def _read_response_fallback(response: Mapping[str, Any], language: str) -> str:
    """Provide a useful deterministic summary when response generation is unavailable."""
    kind = str(response.get("kind") or "")
    if kind == "wallet_query":
        return _wallet_summary_message(response.get("wallet") or {}, language)
    if kind == "portfolio_query":
        portfolio = response.get("portfolio") or response
        wallet = portfolio.get("wallet") or {}
        assets = portfolio.get("assets") or _wallet_balance_items(wallet)
        total = portfolio.get("total_usd_value")
        if total is None:
            total_value = portfolio.get("total_value") or {}
            if isinstance(total_value, Mapping):
                total = total_value.get("total_value")
        if language == "zh":
            suffix = f"，总价值约 ${total}" if total is not None else ""
            return (
                f"当前资产组合包含 {len(assets)} 项资产{suffix}：\n"
                + "\n".join(
                    "- "
                    f"{(item.get('asset') or {}).get('symbol', '未知资产')}："
                    f"{item.get('amount', '0')}"
                    for item in assets
                )
                if assets
                else "当前钱包没有查到可用余额。"
            )
        suffix = f", worth about ${total} in total" if total is not None else ""
        return (
            f"Your portfolio contains {len(assets)} assets{suffix}:\n"
            + "\n".join(
                "- "
                f"{(item.get('asset') or {}).get('symbol', 'Unknown asset')}: "
                f"{item.get('amount', '0')}"
                for item in assets
            )
            if assets
            else "I couldn't find any available balance in this wallet."
        )
    if kind == "price_query":
        prices = response.get("prices") or []
        if prices:
            first = prices[0]
            asset = first.get("asset") or {}
            return (
                f"{asset.get('symbol', '该资产')} 当前价格约为 ${first.get('usd_price')}。"
                if language == "zh"
                else (
                    f"{asset.get('symbol', 'The asset')} is currently about "
                    f"${first.get('usd_price')}."
                )
            )
    if kind == "asset_value":
        if response.get("message"):
            return str(response["message"])
        asset = response.get("asset") or {}
        symbol = asset.get("symbol") or "资产"
        value = response.get("usd_value")
        if value is not None:
            return (
                f"你的 {symbol} 当前价值约为 ${value}。"
                if language == "zh"
                else f"Your {symbol} is currently worth about ${value}."
            )
        return (
            f"我查到你的 {symbol} 余额，但暂时无法计算美元价值。"
            if language == "zh"
            else f"I found your {symbol} balance, but cannot calculate its USD value right now."
        )
    if kind == "market_details":
        count = len(response.get("details") or [])
        return (
            f"我找到 {count} 条行情详情。"
            if language == "zh"
            else f"I found {count} market detail records."
        )
    if kind == "price_history":
        history = response.get("history") or {}
        candles = history.get("candles") if isinstance(history, Mapping) else None
        count = len(candles or []) if isinstance(candles, list) else 0
        return (
            f"已找到 {count} 条历史价格数据。"
            if language == "zh"
            else f"I found {count} historical price records."
        )
    if kind == "gas_check":
        return str(response.get("message") or "暂时无法完成 Gas 检查。")
    if kind == "asset_discovery":
        count = len(response.get("assets") or [])
        return (
            f"我找到 {count} 个匹配的资产。"
            if language == "zh"
            else f"I found {count} matching assets."
        )
    if kind in {"transaction_status", "swap_status"}:
        return str(response.get("message") or "暂时无法确定交易状态。")
    if kind == "swap_quote":
        count = len(response.get("quotes") or [])
        return (
            f"我找到 {count} 个兑换报价，请选择一个继续。"
            if language == "zh"
            else f"I found {count} swap quotes. Choose one to continue."
        )
    return "请求已处理完成。" if language == "zh" else "The request is complete."


def _confirmation_snapshot(
    *,
    action: str,
    status: str,
    summary: dict[str, Any],
    ttl_seconds: int,
    reason: str | None = None,
    requested_at: datetime | None = None,
    task_id: str | None = None,
    task_revision: int | None = None,
) -> dict[str, Any]:
    now = requested_at or datetime.now(timezone.utc)
    snapshot = {
        "action": action,
        "status": status,
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        "summary": summary,
        "reason": reason,
        "payload_hash": confirmation_payload_hash(summary),
    }
    if task_id is not None:
        snapshot["task_id"] = task_id
    if task_revision is not None:
        snapshot["task_revision"] = task_revision
    return snapshot


def confirmation_payload_hash(summary: Mapping[str, Any]) -> str:
    """Hash a JSON-safe confirmation summary independently of dictionary order."""
    canonical = json.dumps(
        _dump(summary),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _swap_confirmation_summary(
    selected: Mapping[str, Any], *, slippage_bps: int | None = None
) -> dict[str, Any]:
    return {
        "provider": selected.get("provider"),
        "provider_reference": selected.get("provider_reference"),
        "source_asset": selected.get("source_asset"),
        "destination_asset": selected.get("destination_asset"),
        "input_amount": selected.get("input_amount"),
        "expected_output": selected.get("expected_output"),
        "slippage_bps": (
            slippage_bps
            if slippage_bps is not None
            else (selected.get("slippage_bps") if selected.get("slippage_bps") is not None else 100)
        ),
    }


def _confirmation_stale(state: Mapping[str, Any], confirmation: Mapping[str, Any]) -> bool:
    """Return false for legacy confirmations and strictly validate versioned ones."""
    if not all(key in confirmation for key in ("task_id", "task_revision", "payload_hash")):
        return False
    task = hydrate_active_task(state)
    if task is None:
        expected_task_id = f"legacy:{state.get('conversation_id') or 'conversation'}:swap"
        expected_revision = 0
    else:
        expected_task_id = str(task.get("task_id"))
        expected_revision = int(task.get("revision") or 0)
    if confirmation.get("task_id") != expected_task_id:
        return True
    if int(confirmation.get("task_revision") or 0) != expected_revision:
        return True
    selected = _dump(state.get("selected_quote"))
    if not isinstance(selected, Mapping):
        return True
    task_slippage = ((task or {}).get("slots") or {}).get("slippage_bps")
    return confirmation.get("payload_hash") != confirmation_payload_hash(
        _swap_confirmation_summary(selected, slippage_bps=task_slippage)
    )


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


def _looks_like_transfer_message(message: str) -> bool:
    """Recognize explicit transfer language before an unfinished swap can claim it."""
    text = "".join(str(message).lower().split())
    return any(
        token in text
        for token in (
            "转账",
            "转给",
            "转到",
            "给自己转",
            "自己转",
            "发送",
            "transfer",
            "send",
        )
    )


def _looks_like_other_chain_balance_request(message: str) -> bool:
    text = "".join(str(message).lower().split())
    return (
        any(token in text for token in ("其他链", "别的链", "otherchain", "anotherchain"))
        and any(token in text for token in ("余额", "资产", "balance", "portfolio"))
    )


def _looks_like_all_chain_balance_request(message: str) -> bool:
    text = "".join(str(message).lower().split())
    return (
        any(
            token in text
            for token in (
                "所有链",
                "全部链",
                "全链",
                "所有网络",
                "全部网络",
                "allchains",
                "allnetworks",
            )
        )
        and any(token in text for token in ("余额", "资产", "balance", "portfolio"))
    )


_BALANCE_SYMBOL = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,11})(?![A-Za-z0-9])"
)
_BALANCE_SYMBOL_STOP_WORDS = frozenset(
    {
        "ALL",
        "ASSET",
        "ASSETS",
        "BALANCE",
        "BALANCES",
        "CHAIN",
        "CHAINS",
        "CHECK",
        "HOW",
        "MUCH",
        "MY",
        "NETWORK",
        "NETWORKS",
        "OF",
        "ON",
        "OTHER",
        "PLEASE",
        "PORTFOLIO",
        "THE",
        "VALUE",
        "WALLET",
        "WHAT",
        "WORTH",
    }
)
_BALANCE_SYMBOL_ALIASES = {
    "比特币": "BTC",
    "以太币": "ETH",
    "币安币": "BNB",
    "泰达币": "USDT",
}


def _requested_balance_symbol(message: str, supported_chains: set[str]) -> str | None:
    """Extract an asset scope only from the current balance request.

    This intentionally does not consult previous portfolio facts: a fresh
    request such as ``我的 BNB 余额`` must override any previously displayed
    asset list. Network identifiers are excluded unless they are also used in
    an asset-shaped phrase (for example ``ETH 余额``).
    """
    text = str(message or "").strip()
    lowered = text.lower()
    if not any(
        token in lowered
        for token in ("余额", "持有", "有多少", "balance", "holding", "holdings")
    ):
        return None
    for alias, symbol in _BALANCE_SYMBOL_ALIASES.items():
        if alias in text:
            return symbol
    candidates = [canonical_symbol(match) for match in _BALANCE_SYMBOL.findall(text)]
    candidates = [
        symbol
        for symbol in candidates
        if symbol and symbol not in _BALANCE_SYMBOL_STOP_WORDS
    ]
    for symbol in candidates:
        # BSC/Base/etc. are networks in balance requests. ETH and SOL are also
        # asset symbols, so keep them unless the user explicitly writes 链 or
        # network immediately around the identifier.
        if symbol in supported_chains:
            network_pattern = re.compile(
                rf"(?:{re.escape(symbol)}\s*(?:链|网络|chain|network)|"
                rf"(?:链|网络|chain|network)\s*{re.escape(symbol)})",
                re.IGNORECASE,
            )
            if symbol not in {"ETH", "SOL", "TRX", "BNB"} or network_pattern.search(text):
                continue
        return symbol
    return None


def _is_positive_usd_asset(item: Mapping[str, Any]) -> bool:
    """Keep assets unless the provider has explicitly valued them at zero."""
    value = item.get("usd_value")
    if value is None:
        return True
    try:
        return Decimal(str(value)) > 0
    except (ArithmeticError, TypeError, ValueError):
        return False


def _filter_balance_assets(
    assets: list[Mapping[str, Any]],
    requested_symbol: str | None,
    *,
    positive_only: bool = False,
) -> list[Mapping[str, Any]]:
    symbol = canonical_symbol(requested_symbol or "")
    return [
        item
        for item in assets
        if (not positive_only or _is_positive_usd_asset(item))
        and (
            not symbol
            or canonical_symbol(str((item.get("asset") or {}).get("symbol") or ""))
            == symbol
        )
    ]


def _explicit_balance_chain(message: str, supported_chains: set[str]) -> str | None:
    text = "".join(str(message).upper().split())
    aliases = {
        "ETH": ("ETH", "ETHEREUM", "ERC20", "以太", "以太坊"),
        "BSC": ("BSC", "BEP20", "币安链", "币安智能链"),
        "BASE": ("BASE",),
        "ARBITRUM": ("ARBITRUM",),
        "OPTIMISM": ("OPTIMISM", "OPMAINNET"),
        "POLYGON": ("POLYGON",),
        "TRON": ("TRON", "TRX", "波场"),
        "SOLANA": ("SOLANA", "SOL", "索拉纳"),
    }
    for chain in supported_chains:
        candidates = aliases.get(chain, (chain,))
        for alias in candidates:
            normalized_alias = alias.upper().replace(" ", "")
            if any(ord(char) > 127 for char in normalized_alias):
                if normalized_alias in text:
                    return chain
            elif re.search(
                rf"(?<![A-Z0-9]){re.escape(normalized_alias)}(?![A-Z0-9])",
                text,
            ):
                return chain
    return None


def _mentioned_balance_chain(message: str) -> str | None:
    """Recognize common network names, including unsupported ones."""
    text = "".join(str(message).upper().split())
    aliases = {
        "BTC": ("BTC", "BITCOIN", "比特币"),
        "ETH": ("ETH", "ETHEREUM", "ERC20", "以太", "以太坊"),
        "BSC": ("BSC", "BEP20", "币安链", "币安智能链"),
        "BASE": ("BASE",),
        "ARBITRUM": ("ARBITRUM",),
        "OPTIMISM": ("OPTIMISM", "OPMAINNET"),
        "POLYGON": ("POLYGON",),
        "TRON": ("TRON", "TRX", "波场"),
        "SOLANA": ("SOLANA", "SOL", "索拉纳"),
    }
    for chain, candidates in aliases.items():
        for alias in candidates:
            normalized = alias.upper().replace(" ", "")
            if any(ord(char) > 127 for char in normalized):
                if normalized in text:
                    return chain
            elif re.search(
                rf"(?<![A-Z0-9]){re.escape(normalized)}(?![A-Z0-9])", text
            ):
                return chain
    return None


def _looks_like_swap_status_message(message: str) -> bool:
    """Recognize a status question about a previously displayed swap."""
    text = "".join(str(message).lower().split())
    return (
        any(token in text for token in ("兑换", "换的", "swap"))
        and any(token in text for token in ("到账", "状态", "完成", "现在", "刚才"))
    )


def _operation_snapshot(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Capture restart-safe lifecycle data before a new operation replaces the projection."""
    orders = _dump(state.get("provider_orders") or {})
    broadcast_hash = state.get("broadcast_tx_hash")
    selected_quote = _dump(state.get("selected_quote"))
    if not orders and not broadcast_hash and not selected_quote:
        return None
    task = _dump(state.get("active_task") or {})
    return {
        "task_id": task.get("task_id"),
        "kind": task.get("kind") or "swap",
        "status": task.get("status"),
        "stage": task.get("stage"),
        "task": task,
        "selected_quote": selected_quote,
        "provider_orders": orders if isinstance(orders, Mapping) else {},
        "provider_order_ids": _dump(state.get("provider_order_ids") or {}),
        "status_snapshot": _dump(state.get("status_snapshot")),
        "broadcast_tx_hash": str(broadcast_hash) if broadcast_hash else None,
        "broadcast_status": state.get("broadcast_status"),
        "confirmation_state": _dump(state.get("confirmation_state")),
    }


def _latest_historical_swap(history: Any) -> dict[str, Any] | None:
    for item in reversed(history or []):
        if not isinstance(item, Mapping) or str(item.get("kind") or "") != "swap":
            continue
        if item.get("provider_orders") or item.get("broadcast_tx_hash"):
            return dict(item)
    return None


def _active_task_followup_intent(
    active_task: Any, message: str, suggestion_data: Any
) -> str | None:
    """Reuse an unfinished task only for explicit, task-shaped follow-up input."""
    if not isinstance(active_task, Mapping) or active_task.get("status") not in {
        "collecting",
        "ready",
    }:
        return None
    kind = active_task.get("kind")
    if isinstance(suggestion_data, Mapping):
        suggested_intent = suggestion_data.get("intent")
        if kind == "swap" and suggested_intent == "swap_quote":
            return "swap_quote"
        if kind == "transfer" and suggested_intent == "transfer":
            return "transfer"
    if kind == "swap" and (
        _looks_like_swap_message(message)
        or bool(swap_direction_hints(message))
        or mentioned_amount_with_unit(message) is not None
        or unambiguous_amount_with_unit(message) is not None
    ):
        return "swap_quote"
    if kind == "transfer" and (
        any(token in message.lower() for token in ("transfer", "send", "转账", "转给"))
        or mentioned_amount_with_unit(message) is not None
        or unambiguous_amount_with_unit(message) is not None
        or bool(re.search(r"0x[0-9a-fA-F]{40}", message))
        or any(
            token in message.lower()
            for token in (
                "自己",
                "当前钱包",
                "钱包地址",
                "收款地址",
                "bsc",
                "bnb chain",
                "usdc",
                "usdt",
                "eth",
            )
        )
    ):
        return "transfer"
    return None


def _self_recipient_hint(message: str) -> bool:
    text = "".join(str(message).lower().split())
    return any(
        phrase in text
        for phrase in (
            "给自己",
            "转给自己",
            "转到自己的地址",
            "当前钱包地址",
            "我的钱包地址",
            "myself",
            "mywallet",
        )
    )


def _recent_fiat_value(history: Any) -> tuple[str, str] | None:
    if not isinstance(history, list):
        return None
    for item in reversed(history):
        if not isinstance(item, Mapping) or item.get("role") != "user":
            continue
        hint = mentioned_fiat_value(str(item.get("content") or ""))
        if hint is not None:
            return hint
    return None


def _parse_slippage_bps(message: str) -> int | None:
    """Parse an explicitly stated percentage or basis-point slippage value."""
    match = re.search(
        r"(?:滑点|slippage)\s*(?:设为|设置为|调整为|为|=|:)??\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*(%|百分比|bps?|基点)?",
        str(message),
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        value = Decimal(match.group(1))
        unit = (match.group(2) or "%").lower()
        bps = value if unit in {"bps", "bp", "基点"} else value * Decimal(100)
        if bps != bps.to_integral_value() or bps < 0 or bps > 10_000:
            return None
        return int(bps)
    except (ArithmeticError, ValueError):
        return None


def _looks_like_cancel_message(message: str) -> bool:
    text = "".join(str(message).lower().split())
    return any(
        token in text
        for token in (
            "取消兑换",
            "取消交易",
            "停止兑换",
            "停止交易",
            "算了",
            "放弃",
            "不用了",
            "不换了",
            "别换了",
            "撤销",
            "cancelswap",
            "cancel",
            "never mind",
            "nevermind",
            "abort",
        )
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
    for side in ("source", "destination"):
        chain_key = f"{side}_chain"
        symbol_key = f"{side}_symbol"
        token_key = f"{side}_token_address"
        if (
            chain_key in incoming
            and symbol_key in incoming
            and token_key not in incoming
            and _native_swap_asset(str(merged.get(chain_key)), str(merged.get(symbol_key)))
            is not None
        ):
            merged.pop(token_key, None)
            merged.pop(f"{side}_decimals", None)
            for field in (f"{side}_chain_id", f"{side}_name", f"{side}_logo_url"):
                merged.pop(field, None)
    return merged


def _swap_draft_from_request(request: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten a request so an older checkpoint can re-enter slot filling."""
    raw = _dump(request) or {}
    if not isinstance(raw, dict):
        return {}
    draft: dict[str, Any] = {
        key: raw.get(key)
        for key in (
            "input_amount",
            "input_amount_raw",
            "output_amount",
            "amount_mode",
            "sender_address",
            "recipient_address",
        )
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
        "output_amount": "目标到账数量",
        "sender_address": "钱包地址",
        "recipient_address": "接收地址",
    }
    if swap and missing:
        if missing == ["input_amount"] and message is None:
            source_symbol = "USDC"
            return f"你要使用的 {source_symbol} 数量是多少？"
        readable = [labels.get(item, item) for item in missing]
        return f"可以帮你兑换。还需要确认：{'、'.join(readable)}。"
    if message:
        return message
    return (
        "你好！我可以帮你查询余额、比较兑换报价、发起转账或兑换。请告诉我具体的 Token、链和数量。"
    )


def _swap_suggestions(
    draft: Mapping[str, Any],
    missing: list[str],
    wallet_context: Mapping[str, Any] | None,
    providers: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Offer explicit replies that fill slots without taking an action for the user."""
    suggestions: list[dict[str, str]] = []
    source_symbol = str(draft.get("source_symbol") or "").strip()
    destination_symbol = str(draft.get("destination_symbol") or "").strip()
    source_chain = str(draft.get("source_chain") or "").strip()
    destination_chain = str(draft.get("destination_chain") or "").strip()
    wallet_chain = str((wallet_context or {}).get("chain") or "").strip()

    if wallet_chain and all(field in missing for field in ("source_chain", "destination_chain")):
        suggestions.append(
            {"label": f"都在 {wallet_chain} 网络", "message": f"都在 {wallet_chain} 链"}
        )
    elif "source_chain" in missing and destination_chain:
        suggestions.append(
            {
                "label": f"来源也在 {destination_chain} 网络",
                "message": f"来源也在 {destination_chain} 链",
            }
        )
    elif "destination_chain" in missing and source_chain:
        suggestions.append(
            {
                "label": f"目标也在 {source_chain} 网络",
                "message": f"目标也在 {source_chain} 链",
            }
        )
    if "source_symbol" in missing and destination_symbol:
        suggestions.append(
            {
                "label": f"示例：用 USDC 换 {destination_symbol}",
                "message": "用当前网络上的 USDC 换",
            }
        )
    if "destination_symbol" in missing and source_symbol:
        suggestions.append(
            {
                "label": f"示例：把 {source_symbol} 换成 USDT",
                "message": "换成当前网络上的 USDT",
            }
        )
    if "input_amount" in missing and source_symbol:
        minimums = []
        for provider in (providers or {}).values():
            value = getattr(provider, "minimum_source_amount", None)
            if value is None:
                value = getattr(provider, "deposit_min", None)
            if value not in (None, ""):
                minimums.append(str(value))
        if minimums:
            message = f"{minimums[0]} {source_symbol}"
            label = f"最低 {message}"
        else:
            message = f"请输入 {source_symbol} 数量"
            label = f"填写 {source_symbol} 数量"
        suggestions.append({"label": label, "message": message})
    return suggestions[:3]


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
    "output_amount",
    "amount_mode",
    "target_value_amount",
    "target_value_currency",
    "target_value_price_usd",
    "target_value_observed_at",
    "target_value_provider",
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


_NATIVE_ASSET_DECIMALS = {
    "ETH": 18,
    "BASE": 18,
    "ARBITRUM": 18,
    "OPTIMISM": 18,
    "BSC": 18,
    "POLYGON": 18,
    "TRON": 6,
    "SOLANA": 9,
}


def _unique_native_chain(symbol: str, explicit_address: Any = None) -> str | None:
    """Return a chain only when a symbol identifies one supported native asset."""
    if _explicit_token_address(explicit_address):
        return None
    canonical_symbol_name = canonical_symbol(symbol)
    matches = [
        chain
        for chain in _NATIVE_ASSET_DECIMALS
        if _native_symbol(chain, None) == canonical_symbol_name
    ]
    return matches[0] if len(matches) == 1 else None


def _reconcile_unique_native_chains(slots: dict[str, Any]) -> dict[str, Any]:
    """Correct chain guesses for uniquely identifiable native assets."""
    reconciled = dict(slots)
    for side in ("source", "destination"):
        symbol = reconciled.get(f"{side}_symbol")
        if not symbol:
            continue
        inferred_chain = _unique_native_chain(
            str(symbol), reconciled.get(f"{side}_token_address")
        )
        if inferred_chain is None:
            continue
        chain_key = f"{side}_chain"
        if canonical_chain(str(reconciled.get(chain_key) or "")) == inferred_chain:
            continue
        reconciled = _merge_swap_slots(reconciled, {chain_key: inferred_chain})
    return reconciled


def _native_swap_asset(chain: str, symbol: str) -> Asset | None:
    canonical_chain_name = canonical_chain(chain)
    canonical_symbol_name = canonical_symbol(symbol)
    if _native_symbol(canonical_chain_name, None) != canonical_symbol_name:
        return None
    decimals = _NATIVE_ASSET_DECIMALS.get(canonical_chain_name)
    if decimals is None:
        return None
    return Asset(
        chain=canonical_chain_name,
        chain_id=chain_id_for(canonical_chain_name),
        symbol=canonical_symbol_name,
        decimals=decimals,
        address=None,
    )


def _explicit_token_address(value: Any) -> str | None:
    address = str(value).strip() if value is not None else ""
    return address or None


def _sanitize_native_transfer_draft(
    draft: Mapping[str, Any], wallet_context: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Remove stale ERC-20 metadata when a persisted transfer is native."""
    resolved = dict(draft)
    context = wallet_context or {}
    chain = resolved.get("transfer_chain") or resolved.get("chain") or context.get("chain")
    symbol = resolved.get("transfer_symbol") or resolved.get("symbol")
    if not chain or not symbol:
        return resolved
    canonical_chain_name = canonical_chain(str(chain))
    canonical_symbol_name = canonical_symbol(str(symbol))
    if _native_symbol(canonical_chain_name, None) != canonical_symbol_name:
        return resolved
    for key in (
        "token_address",
        "transfer_token_address",
        "decimals",
        "transfer_decimals",
        "chain_id",
        "transfer_chain_id",
        "amount_raw",
        "transfer_amount_raw",
    ):
        resolved.pop(key, None)
    resolved["transfer_chain"] = canonical_chain_name
    resolved["transfer_symbol"] = canonical_symbol_name
    return resolved


def _sanitize_native_transfer_task(
    task: dict[str, Any] | None, wallet_context: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    if not task or task.get("kind") != "transfer":
        return task
    slots = dict(task.get("slots") or {})
    chain = slots.get("chain") or slots.get("transfer_chain") or (wallet_context or {}).get("chain")
    symbol = slots.get("symbol") or slots.get("transfer_symbol")
    if not chain or not symbol:
        return task
    if _native_symbol(canonical_chain(str(chain)), None) != canonical_symbol(str(symbol)):
        return task
    sanitized = dict(slots)
    for key in (
        "token_address",
        "transfer_token_address",
        "decimals",
        "transfer_decimals",
        "chain_id",
        "transfer_chain_id",
        "amount_raw",
        "transfer_amount_raw",
    ):
        sanitized.pop(key, None)
    if sanitized == slots:
        return task
    sources = {
        key: value
        for key, value in dict(task.get("slot_sources") or {}).items()
        if key in sanitized
    }
    return {**task, "slots": sanitized, "slot_sources": sources, "updated_by": "system"}


_COMMON_TRANSFER_ASSETS: dict[tuple[str, str], Asset] = {
    ("ETH", "USDC"): Asset(
        chain="ETH",
        chain_id=1,
        symbol="USDC",
        decimals=6,
        address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        name="USD Coin",
    ),
    ("ETH", "USDT"): Asset(
        chain="ETH",
        chain_id=1,
        symbol="USDT",
        decimals=6,
        address="0xdAC17F958D2ee523a2206206994597C13D831ec7",
        name="Tether USD",
    ),
}


async def _resolve_transfer_asset(
    draft: dict[str, Any],
    providers: Mapping[str, Any],
    wallet_context: Mapping[str, Any] | None = None,
    wallet_provider: Any | None = None,
) -> dict[str, Any]:
    """Fill transfer token metadata from trusted common assets or providers."""
    resolved = dict(draft)
    context = wallet_context or {}
    chain = resolved.get("transfer_chain") or resolved.get("chain") or context.get("chain")
    symbol = resolved.get("transfer_symbol") or resolved.get("symbol")
    if not chain or not symbol:
        return resolved
    canonical_chain_name = canonical_chain(str(chain))
    canonical_symbol_name = canonical_symbol(str(symbol))
    if _native_symbol(canonical_chain_name, None) == canonical_symbol_name:
        return _sanitize_native_transfer_draft(resolved, context)
    if resolved.get("transfer_token_address") and resolved.get("transfer_decimals") is not None:
        return resolved

    asset = _COMMON_TRANSFER_ASSETS.get((canonical_chain_name, canonical_symbol_name))
    # A wallet balance is a trusted, chain-specific source of token metadata.
    # Prefer it before provider discovery so a connected wallet can complete a
    # transfer without asking the user to paste a contract and decimals.
    if asset is None:
        raw_balances = context.get("token_balances") or context.get("assets") or []
        if (
            not raw_balances
            and wallet_provider is not None
            and hasattr(wallet_provider, "get_token_balances")
            and context.get("address")
        ):
            chain_indexes = getattr(wallet_provider, "chain_index_by_name", {}) or {}
            chain_index = chain_indexes.get(canonical_chain_name)
            if chain_index is not None:
                try:
                    raw_balances = await wallet_provider.get_token_balances(
                        str(context["address"]), [str(chain_index)]
                    )
                except Exception:
                    # Wallet balance reads are an optional metadata source;
                    # provider asset discovery remains the fallback.
                    raw_balances = []
        wallet_matches: list[Asset] = []
        for raw_balance in raw_balances:
            if isinstance(raw_balance, Mapping):
                item = raw_balance.get("asset")
            else:
                item = getattr(raw_balance, "asset", None)
            if hasattr(item, "model_dump"):
                item = item.model_dump(mode="json")
            if not isinstance(item, Mapping):
                continue
            try:
                candidate = Asset.model_validate(item)
            except Exception:
                continue
            if (
                candidate.address
                and canonical_chain(str(candidate.chain)) == canonical_chain_name
                and canonical_symbol(str(candidate.symbol)) == canonical_symbol_name
            ):
                wallet_matches.append(candidate)
        unique_wallet = {
            (str(item.address).lower(), int(item.decimals)): item for item in wallet_matches
        }
        if len(unique_wallet) == 1:
            asset = next(iter(unique_wallet.values()))
    if asset is None:
        matches: list[Asset] = []
        for provider in providers.values():
            if not hasattr(provider, "list_assets"):
                continue
            try:
                matches.extend(
                    await provider.list_assets(
                        AssetQuery(
                            chain=canonical_chain_name,
                            search=canonical_symbol_name,
                        )
                    )
                )
            except Exception:
                continue
        unique = {
            (str(item.address).lower(), int(item.decimals)): item
            for item in matches
            if item.address
            and canonical_chain(str(item.chain)) == canonical_chain_name
            and canonical_symbol(str(item.symbol)) == canonical_symbol_name
        }
        if len(unique) == 1:
            asset = next(iter(unique.values()))
    if asset is None:
        return resolved
    resolved["transfer_chain"] = canonical_chain_name
    resolved["transfer_symbol"] = asset.symbol
    resolved["transfer_token_address"] = asset.address
    resolved["transfer_decimals"] = asset.decimals
    if asset.chain_id is not None:
        resolved["transfer_chain_id"] = asset.chain_id
    return resolved


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
    wallet_provider: Any | None = None,
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
            try:
                fee = await adapter.estimate_fee(
                    to=transaction.to,
                    data=transaction.data,
                    from_address=sender or None,
                    value=getattr(transaction, "value", None),
                )
            except TypeError:
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

    gas_sources: dict[str, Any] = {
        "rpc": _fee_dump(fee).get("gas_limit") if fee is not None else None,
        "okx": None,
        "simulation": None,
    }
    if wallet_provider is not None:
        if not hasattr(wallet_provider, "estimate_gas_limit"):
            gas_sources["okx"] = "unavailable"
        if not hasattr(wallet_provider, "simulate_transaction"):
            gas_sources["simulation"] = "unavailable"
    simulation = None
    if wallet_provider is not None:
        tx_context = TransactionContext(
            chain=chain,
            from_address=sender,
            to_address=recipient,
            native_amount=_transaction_native_amount(getattr(transaction, "value", "0")),
            calldata=str(getattr(transaction, "data", "0x")),
        )
        if hasattr(wallet_provider, "estimate_gas_limit"):
            try:
                estimate = await wallet_provider.estimate_gas_limit(tx_context)
                gas_sources["okx"] = str(estimate.gas_limit)
            except Exception:
                gas_sources["okx"] = "unavailable"
                add(
                    "okx_gas",
                    "warning",
                    "OKX gas-limit 预检查暂时不可用。",
                    code="OKX_GAS_UNAVAILABLE",
                )
        if hasattr(wallet_provider, "simulate_transaction"):
            try:
                simulation = await wallet_provider.simulate_transaction(tx_context)
                if simulation.gas_used is not None:
                    gas_sources["simulation"] = str(simulation.gas_used)
                if simulation.success:
                    add("simulation", "passed", "OKX 交易模拟通过。")
                else:
                    failure_reason = simulation.failure_reason or ""
                    if "insufficient funds" in failure_reason.lower():
                        add(
                            "balance",
                            "failed",
                            "模拟显示原生币余额不足以支付交易金额和网络手续费。",
                            code="INSUFFICIENT_BALANCE",
                            details={"failure_reason": failure_reason},
                        )
                    add(
                        "simulation",
                        "failed",
                        "交易模拟失败，签名前请检查交易参数。",
                        code="SIMULATION_FAILED",
                        details={"failure_reason": simulation.failure_reason},
                    )
            except Exception:
                gas_sources["simulation"] = "unavailable"
                add(
                    "simulation",
                    "warning",
                    "OKX 交易模拟暂时不可用，签名前请在钱包中确认。",
                    code="SIMULATION_UNAVAILABLE",
                )
    if fee is not None and any(
        isinstance(gas_sources.get(key), str)
        and gas_sources[key].isdigit()
        for key in ("okx", "simulation")
    ):
        values = [
            int(value)
            for value in (
                gas_sources.get("rpc"),
                gas_sources.get("okx"),
                gas_sources.get("simulation"),
            )
            if isinstance(value, str) and value.isdigit()
        ]
        fee = _fee_dump(fee)
        fee["gas_limit"] = str(max(values))
        if fee.get("max_fee_per_gas"):
            fee["amount_raw"] = str(int(fee["gas_limit"]) * int(fee["max_fee_per_gas"]))

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
            fee_raw = (
                int(fee["amount_raw"] if isinstance(fee, dict) else fee.amount_raw)
                if fee is not None
                else 0
            )
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
                if not any(
                    item["name"] == "balance" and item["status"] == "failed"
                    for item in checks
                ):
                    add("balance", "passed", "原生币余额足够覆盖金额和网络手续费。")
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
            fee_raw = int(fee["amount_raw"] if isinstance(fee, dict) else fee.amount_raw)
            if int(native_balance.amount_raw) < fee_raw:
                add(
                    "gas_balance",
                    "failed",
                    "原生币余额不足以支付网络手续费。",
                    code="INSUFFICIENT_GAS",
                    details={"balance_raw": native_balance.amount_raw, "fee_raw": str(fee_raw)},
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
        "fee_estimate": (
            fee if isinstance(fee, dict) else (_fee_dump(fee) if fee is not None else None)
        ),
        "gas_sources": gas_sources,
        **({"simulation": _dump(simulation)} if simulation is not None else {}),
        "checks": checks,
        "warnings": [item for item in checks if item["status"] == "warning"],
    }


def _swap_draft_request(
    draft: dict[str, Any], wallet_context: dict[str, Any] | None
) -> tuple[SwapQuoteRequest | None, list[str]]:
    context = wallet_context or {}
    normalized = dict(draft)
    normalized.setdefault("slippage_bps", 100)
    normalized.setdefault("sender_address", context.get("address"))
    normalized.setdefault("recipient_address", context.get("address"))
    required = [
        "source_chain",
        "destination_chain",
        "source_symbol",
        "destination_symbol",
        "source_decimals",
        "destination_decimals",
        "sender_address",
        "recipient_address",
    ]
    for side in ("source", "destination"):
        chain = normalized.get(f"{side}_chain")
        symbol = normalized.get(f"{side}_symbol")
        token_address = _explicit_token_address(normalized.get(f"{side}_token_address"))
        is_native = (
            chain
            and symbol
            and not token_address
            and _native_swap_asset(str(chain), str(symbol)) is not None
        )
        if chain and symbol and not is_native:
            required.append(f"{side}_token_address")
    exact_out = normalized.get("output_amount") not in (None, "") and not normalized.get(
        "input_amount"
    )
    if not exact_out:
        required.append("input_amount")
    missing = [field for field in required if normalized.get(field) in (None, "")]
    if missing:
        return None, missing
    if exact_out:
        try:
            target = Decimal(str(normalized["output_amount"]))
            if target <= 0:
                raise ValueError
            # Reverse-capable providers replace this provisional amount before
            # forwarding the quote; it only lets the domain model carry assets.
            normalized["input_amount"] = "1"
            normalized["input_amount_raw"] = "1"
        except Exception:
            return None, ["output_amount"]
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
    source_address = _explicit_token_address(normalized.get("source_token_address"))
    destination_address = _explicit_token_address(normalized.get("destination_token_address"))
    source_native = None
    if not source_address:
        source_native = _native_swap_asset(
            str(normalized["source_chain"]), str(normalized["source_symbol"])
        )
    destination_native = None
    if not destination_address:
        destination_native = _native_swap_asset(
            str(normalized["destination_chain"]), str(normalized["destination_symbol"])
        )
    same_chain = canonical_chain(str(normalized["source_chain"])) == canonical_chain(
        str(normalized["destination_chain"])
    )
    same_native_asset = (
        same_chain
        and source_native is not None
        and destination_native is not None
        and canonical_symbol(str(normalized["source_symbol"]))
        == canonical_symbol(str(normalized["destination_symbol"]))
    )
    same_contract_asset = (
        same_chain
        and source_address is not None
        and destination_address is not None
        and str(source_address).strip().lower() == str(destination_address).strip().lower()
    )
    if same_native_asset or same_contract_asset:
        return None, ["source_equals_destination"]
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
            "address": source_address,
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
            "address": destination_address,
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
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fill token metadata when a provider has exactly one matching asset."""
    resolution_started = perf_counter()
    _emit_progress(
        "asset_resolution",
        "started",
        message="正在解析兑换资产。",
    )
    resolved = dict(draft)
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    asset_providers = {
        name: provider for name, provider in providers.items() if hasattr(provider, "list_assets")
    }
    resolved = _reconcile_unique_native_chains(resolved)
    for side in ("source", "destination"):
        chain = resolved.get(f"{side}_chain")
        symbol = resolved.get(f"{side}_symbol")
        if not chain or not symbol:
            continue
        if _explicit_token_address(resolved.get(f"{side}_token_address")):
            continue
        native_asset = _native_swap_asset(str(chain), str(symbol))
        if native_asset is None:
            continue
        resolved[f"{side}_chain"] = native_asset.chain
        resolved[f"{side}_symbol"] = native_asset.symbol
        resolved[f"{side}_decimals"] = native_asset.decimals
        if native_asset.chain_id is not None:
            resolved[f"{side}_chain_id"] = native_asset.chain_id
        resolved.pop(f"{side}_token_address", None)
    resolvable_sides = [
        side
        for side in ("source", "destination")
        if resolved.get(f"{side}_chain")
        and resolved.get(f"{side}_symbol")
        and (
            _explicit_token_address(resolved.get(f"{side}_token_address"))
            or _native_swap_asset(str(resolved[f"{side}_chain"]), str(resolved[f"{side}_symbol"]))
            is None
        )
        and not (
            resolved.get(f"{side}_token_address") and resolved.get(f"{side}_decimals") is not None
        )
    ]
    if resolvable_sides and not asset_providers:
        _emit_progress(
            "asset_resolution",
            "failed",
            started=resolution_started,
            message="没有可用的 Token 元数据源。",
            error_code="ASSET_PROVIDER_UNAVAILABLE",
        )
        return (
            resolved,
            candidates,
            [_error("ASSET_PROVIDER_UNAVAILABLE", "没有可用的 Token 元数据源。")],
        )
    for side in ("source", "destination"):
        chain = resolved.get(f"{side}_chain")
        symbol = resolved.get(f"{side}_symbol")
        if not chain or not symbol:
            continue
        explicit_address = _explicit_token_address(resolved.get(f"{side}_token_address"))
        if not explicit_address and _native_swap_asset(str(chain), str(symbol)) is not None:
            continue
        if explicit_address and resolved.get(f"{side}_decimals") is not None:
            continue
        matches: list[Asset] = []
        query = AssetQuery(chain=canonical_chain(str(chain)), search=canonical_symbol(str(symbol)))
        for provider_name, provider in asset_providers.items():
            provider_started = perf_counter()
            _emit_progress(
                "asset_discovery",
                "started",
                message=f"正在通过 {provider_name} 查询 {chain} 上的 {symbol}。",
                provider=provider_name,
                side=side,
                chain=canonical_chain(str(chain)),
                symbol=canonical_symbol(str(symbol)),
            )
            try:
                provider_matches = await provider.list_assets(query)
                matches.extend(provider_matches)
                _emit_progress(
                    "asset_discovery",
                    "completed",
                    started=provider_started,
                    message=f"{provider_name} 资产查询完成。",
                    provider=provider_name,
                    side=side,
                    result_count=len(provider_matches),
                )
            except Exception as exc:
                _emit_progress(
                    "asset_discovery",
                    "failed",
                    started=provider_started,
                    message=f"{provider_name} 资产查询失败。",
                    provider=provider_name,
                    side=side,
                    error_code="ASSET_DISCOVERY_FAILED",
                )
                errors.append(
                    _error(
                        "ASSET_DISCOVERY_FAILED",
                        str(exc),
                        retryable=True,
                        details={"provider": provider_name, "side": side},
                    )
                )
        unique = {
            (str(asset.address).lower(), int(asset.decimals)): asset
            for asset in matches
            if asset.address
            and canonical_chain(str(asset.chain)) == canonical_chain(str(chain))
            and canonical_symbol(str(asset.symbol)) == canonical_symbol(str(symbol))
            and (
                explicit_address is None
                or str(asset.address).strip().lower() == explicit_address.lower()
            )
        }
        if len(unique) == 1:
            asset = next(iter(unique.values()))
            resolved[f"{side}_token_address"] = asset.address
            resolved[f"{side}_decimals"] = asset.decimals
            chain_id = asset.chain_id or chain_id_for(str(chain))
            if chain_id is not None:
                resolved[f"{side}_chain_id"] = chain_id
            if asset.name:
                resolved[f"{side}_name"] = asset.name
            if asset.logo_url:
                resolved[f"{side}_logo_url"] = asset.logo_url
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
        else:
            errors.append(
                _error(
                    "ASSET_NOT_FOUND",
                    f"没有找到 {chain} 上的 {symbol}。",
                    details={"side": side, "chain": str(chain), "symbol": str(symbol)},
                )
            )
    _emit_progress(
        "asset_resolution",
        "completed" if not errors else "completed_with_errors",
        started=resolution_started,
        message="兑换资产解析完成。",
        candidate_count=len(candidates),
        error_count=len(errors),
    )
    return resolved, candidates, errors


async def _resolve_swap_target_value(
    draft: dict[str, Any], price_provider: Any | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convert a USD target into an exact destination-asset amount."""
    raw_value = draft.get("target_value_amount")
    if raw_value in (None, ""):
        return draft, []
    required = ("destination_chain", "destination_symbol", "destination_decimals")
    if any(draft.get(field) in (None, "") for field in required):
        return draft, []

    started = perf_counter()
    _emit_progress(
        "fiat_conversion",
        "started",
        message="正在按美元价值计算目标资产数量。",
    )
    if str(draft.get("target_value_currency") or "USD").upper() != "USD":
        errors = [_error("FIAT_CURRENCY_UNSUPPORTED", "目前只支持按 USD 价值换算。")]
    elif price_provider is None:
        errors = [
            _error(
                "PRICE_PROVIDER_UNAVAILABLE",
                "当前没有可用的价格服务，无法按美元价值计算目标数量。",
                retryable=True,
            )
        ]
    else:
        errors = []
    if errors:
        _emit_progress(
            "fiat_conversion",
            "failed",
            started=started,
            message="美元价值换算失败。",
            error_code=errors[0]["code"],
        )
        return draft, errors

    try:
        target_value = Decimal(str(raw_value))
        if target_value <= 0:
            raise ValueError("target value must be positive")
        asset = Asset(
            chain=str(draft["destination_chain"]),
            chain_id=draft.get("destination_chain_id"),
            symbol=str(draft["destination_symbol"]),
            decimals=int(draft["destination_decimals"]),
            address=_explicit_token_address(draft.get("destination_token_address")),
        )
        prices = await price_provider.get_prices([asset])
        price = next((item for item in prices if Decimal(str(item.usd_price)) > 0), None)
        if price is None:
            raise ValueError("price is unavailable")
        observed_at = price.observed_at
        if observed_at is not None:
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - observed_at > timedelta(minutes=5):
                raise ValueError("price is stale")
        precision = Decimal(1).scaleb(-asset.decimals)
        output_amount = (target_value / Decimal(str(price.usd_price))).quantize(
            precision, rounding=ROUND_DOWN
        )
        if output_amount <= 0:
            raise ValueError("converted amount is below asset precision")
    except Exception:
        error = _error(
            "FIAT_VALUE_CONVERSION_FAILED",
            "暂时无法取得有效的目标资产价格，请稍后重试或直接提供目标数量。",
            retryable=True,
        )
        _emit_progress(
            "fiat_conversion",
            "failed",
            started=started,
            message="美元价值换算失败。",
            error_code=error["code"],
        )
        return draft, [error]

    resolved = dict(draft)
    resolved.pop("input_amount", None)
    resolved.pop("input_amount_raw", None)
    resolved.update(
        output_amount=format(output_amount, "f"),
        amount_mode="exact_out",
        target_value_amount=format(target_value, "f"),
        target_value_currency="USD",
        target_value_price_usd=format(Decimal(str(price.usd_price)), "f"),
        target_value_observed_at=(
            observed_at.isoformat()
            if observed_at is not None
            else datetime.now(timezone.utc).isoformat()
        ),
        target_value_provider=str(price.provider or "unknown"),
    )
    _emit_progress(
        "fiat_conversion",
        "completed",
        started=started,
        message="美元价值换算完成。",
        destination_symbol=asset.symbol,
    )
    return resolved, []


def make_nodes(runtime: GraphRuntime) -> dict[str, Any]:
    def okx_chain_index(chain: str) -> str | None:
        mapping = getattr(runtime.wallet_provider, "chain_index_by_name", {})
        canonical = str(chain).upper()
        if canonical in mapping:
            return mapping[canonical]
        # Keep injected wallet providers usable in tests and local deployments
        # even when they do not expose OKX's chain-index registry.
        return {
            "ETH": "1",
            "BSC": "56",
            "POLYGON": "137",
            "OPTIMISM": "10",
            "BASE": "8453",
            "ARBITRUM": "42161",
        }.get(canonical)

    def execution_adapter(chain: str) -> Any:
        observer = runtime.execution_observer
        if observer is not None and hasattr(observer, "adapter"):
            try:
                return observer.adapter(chain)
            except Exception:
                return None
        return runtime.chains.get(str(chain).upper())

    def canonical_state(state: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(state)
        result["request"] = dict(state.get("request") or {})
        if not result["request"].get("message") and state.get("message"):
            result["request"]["message"] = state["message"]
        result["wallet_context"] = dict(state.get("wallet_context") or {})
        return result

    async def response_facts(
        state: Mapping[str, Any], *, intent: str, missing: list[str], include_portfolio: bool
    ) -> dict[str, Any]:
        request = dict(state.get("request") or {})
        context = dict(state.get("wallet_context") or {})
        message = str(request.get("message") or "")
        supported_chains = sorted(
            set(getattr(runtime.wallet_provider, "chain_index_by_name", {}) or {})
            | set(runtime.chains)
        )
        facts: dict[str, Any] = {
            "intent": intent,
            "missing_fields": list(missing),
            "user_message": message,
            "language_hint": _response_language(message, state.get("conversation_history")),
            "wallet": {
                "address": context.get("address") or request.get("address"),
                "chain": context.get("chain") or request.get("chain"),
            },
            "supported_chains": supported_chains,
            "task": _dump(state.get("active_task") or {}),
        }
        draft = state.get("swap_draft") or state.get("transfer_draft") or {}
        if isinstance(draft, Mapping) and draft:
            facts["known_slots"] = {
                key: value
                for key, value in draft.items()
                if not any(part in key.lower() for part in ("address", "raw", "decimals"))
            }
        if not include_portfolio:
            if intent == "transfer":
                draft = state.get("transfer_draft") or {}
                chain = draft.get("transfer_chain") or draft.get("chain") or context.get("chain")
                symbol = draft.get("transfer_symbol") or draft.get("symbol")
                facts["supported_assets"] = (
                    [
                        {
                            "chain": chain,
                            "symbol": symbol,
                            "address": draft.get("transfer_token_address"),
                            "decimals": draft.get("transfer_decimals"),
                        }
                    ]
                    if chain and symbol
                    else []
                )
            return facts

        address = str(context.get("address") or request.get("address") or "")
        indexes = sorted(
            {
                str(index)
                for index in (
                    getattr(runtime.wallet_provider, "chain_index_by_name", {}) or {}
                ).values()
            }
        )
        balances: list[Any] = []
        assets: list[Asset] = []
        if address and indexes and hasattr(runtime.wallet_provider, "get_token_balances"):
            try:
                balances = await runtime.wallet_provider.get_token_balances(address, indexes)
                assets = [balance.asset for balance in balances]
            except Exception:
                balances = []
        supported_assets: dict[tuple[str, str], Asset] = {
            (str(asset.chain).upper(), str(asset.symbol).upper()): asset for asset in assets
        }
        if intent == "swap_quote":
            draft = state.get("swap_draft") or {}
            existing_assets = list(facts.get("supported_assets") or [])
            for side in ("source", "destination"):
                chain = draft.get(f"{side}_chain")
                symbol = draft.get(f"{side}_symbol")
                if chain and symbol:
                    supported_assets.setdefault(
                        (str(chain).upper(), str(symbol).upper()),
                        Asset(
                            chain=str(chain),
                            symbol=str(symbol),
                            decimals=int(draft.get(f"{side}_decimals") or 18),
                            address=draft.get(f"{side}_token_address"),
                        ),
                    )
            existing_assets.extend(
                {
                    "chain": asset.chain,
                    "symbol": asset.symbol,
                    "address": asset.address,
                    "decimals": asset.decimals,
                }
                for asset in supported_assets.values()
            )
            facts["supported_assets"] = existing_assets

            async def provider_assets(provider: Any) -> list[Asset]:
                if not hasattr(provider, "list_assets"):
                    return []
                try:
                    return await provider.list_assets(AssetQuery())
                except Exception:
                    return []

            catalog_results = await asyncio.gather(
                *(provider_assets(provider) for provider in runtime.providers.values())
            )
            for group in catalog_results:
                for asset in group:
                    supported_assets.setdefault(
                        (str(asset.chain).upper(), str(asset.symbol).upper()), asset
                    )
            facts["supported_assets"] = [
                {
                    "chain": asset.chain,
                    "symbol": asset.symbol,
                    "address": asset.address,
                    "decimals": asset.decimals,
                }
                for asset in list(supported_assets.values())[:100]
            ]
        price_map: dict[tuple[str, str, str], Any] = {}
        if runtime.price_provider is not None and assets:
            try:
                prices = await runtime.price_provider.get_prices(assets[:50])
                price_map = {
                    (
                        str(price.asset.chain).upper(),
                        str(price.asset.symbol).upper(),
                        str(price.asset.address or "").lower(),
                    ): price
                    for price in prices
                }
            except Exception:
                price_map = {}
        balance_facts = []
        for balance in balances:
            asset = balance.asset
            price = price_map.get(
                (
                    str(asset.chain).upper(),
                    str(asset.symbol).upper(),
                    str(asset.address or "").lower(),
                )
            )
            balance_facts.append(
                {
                    "chain": asset.chain,
                    "symbol": asset.symbol,
                    "address": asset.address,
                    "decimals": asset.decimals,
                    "amount": str(balance.amount),
                    "usd_price": str(price.usd_price) if price else None,
                }
            )
        facts["balances"] = balance_facts[:50]
        facts["supported_assets"] = [
            {
                "chain": asset.chain,
                "symbol": asset.symbol,
                "address": asset.address,
                "decimals": asset.decimals,
            }
            for asset in list(supported_assets.values())[:100]
        ]
        facts["prices"] = [
            {
                "chain": price.asset.chain,
                "symbol": price.asset.symbol,
                "usd_price": str(price.usd_price),
                "observed_at": price.observed_at.isoformat() if price.observed_at else None,
            }
            for price in price_map.values()
        ]
        return facts

    async def generate_clarification(
        state: Mapping[str, Any],
        *,
        intent: str,
        missing: list[str],
        include_portfolio: bool = False,
    ) -> dict[str, Any]:
        response_started = perf_counter()
        _emit_progress(
            "response_generation",
            "started",
            message="正在生成回复。",
        )
        request = dict(state.get("request") or {})
        if not request.get("message") and state.get("message"):
            request["message"] = state.get("message")
        # Once at least one swap slot is known, the graph can ask precisely for
        # the remaining fields without another model round trip. Open-ended
        # swap prompts still use portfolio facts and the response model, while
        # transfer wording keeps its existing model-generated contract.
        deterministic = bool(missing) and intent == "swap_quote" and not include_portfolio
        facts = await response_facts(
            state,
            intent=intent,
            missing=missing,
            include_portfolio=include_portfolio and not deterministic,
        )
        language = str(facts["language_hint"])
        message = (
            _swap_clarification_message(state, missing, language)
            if deterministic and intent == "swap_quote"
            else _response_fallback(intent, missing, language)
        )
        suggestions: list[dict[str, Any]] = []
        responder = getattr(runtime.model, "respond", None)
        response_strategy = "deterministic" if deterministic else "fallback"
        if callable(responder) and not deterministic:
            try:
                draft = await responder(request, facts)
                draft = _dump(draft) or {}
                if isinstance(draft, Mapping) and str(draft.get("message") or "").strip():
                    response_language = str(draft.get("language") or facts["language_hint"])
                    if response_language.split("-")[0].lower() == facts["language_hint"]:
                        message = str(draft["message"]).strip()
                    suggestions = _sanitize_response_suggestions(
                        draft.get("suggestions"), intent=intent, facts=facts
                    )
                    response_strategy = "model"
            except Exception:
                response_strategy = "fallback"
        if not suggestions and intent == "swap_quote":
            suggestions = _swap_suggestions(
                state.get("swap_draft") or {},
                missing,
                state.get("wallet_context"),
                runtime.providers,
            )
            suggestions = [
                {
                    **suggestion,
                    "data": {
                        "intent": "swap_quote",
                        **{
                            key: value
                            for key, value in (state.get("swap_draft") or {}).items()
                            if key in _RESPONSE_DATA_KEYS["swap_quote"]
                        },
                    },
                }
                for suggestion in suggestions
            ]
            suggestions = _sanitize_response_suggestions(suggestions, intent=intent, facts=facts)
        if not suggestions and intent == "transfer":
            suggestions = await transfer_suggestions(state, missing)
        duration_ms = (perf_counter() - response_started) * 1000
        wallet_event(
            "response_generation",
            "success",
            duration_ms=duration_ms,
            state=state,
        )
        _emit_progress(
            "response_generation",
            "completed",
            started=response_started,
            message="回复生成完成。",
            strategy=response_strategy,
        )
        return {
            "kind": "clarification",
            "message": message,
            "missing_fields": list(missing),
            "suggestions": suggestions,
        }

    async def transfer_suggestions(
        state: Mapping[str, Any], missing: list[str]
    ) -> list[dict[str, Any]]:
        draft = state.get("transfer_draft") or {}
        facts = await response_facts(
            state, intent="transfer", missing=missing, include_portfolio=False
        )
        if not missing:
            return []

        context = state.get("wallet_context") or {}
        chain = (
            draft.get("transfer_chain")
            or draft.get("chain")
            or context.get("chain")
        )
        symbol = draft.get("transfer_symbol") or draft.get("symbol")
        amount = draft.get("transfer_amount") or draft.get("amount")
        base_data = {
            key: value
            for key, value in {
                "intent": "transfer",
                "chain": chain,
                "symbol": symbol,
                "amount": amount,
            }.items()
            if value is not None
        }
        suggestions: list[dict[str, Any]] = []
        language = facts["language_hint"]

        if "transfer_recipient" in missing:
            wallet_address = context.get("address")
            if wallet_address:
                suggestions.append(
                    {
                        "label": "使用当前钱包地址" if language == "zh" else "Use current wallet",
                        "message": (
                            "使用当前连接钱包地址作为收款地址。"
                            if language == "zh"
                            else "Use the connected wallet address as the recipient."
                        ),
                        "data": {**base_data, "recipient": str(wallet_address)},
                    }
                )
            else:
                suggestions.append(
                    {
                        "label": "填写收款地址" if language == "zh" else "Provide recipient",
                        "message": (
                            "请提供收款钱包地址。"
                            if language == "zh"
                            else "Provide the recipient wallet address."
                        ),
                        "data": base_data,
                    }
                )

        if "transfer_token_address" in missing or "transfer_decimals" in missing:
            wallet_assets: list[Mapping[str, Any]] = []
            for raw_balance in context.get("token_balances") or context.get("assets") or []:
                item = raw_balance.get("asset") if isinstance(raw_balance, Mapping) else None
                if isinstance(item, Mapping):
                    wallet_assets.append(item)
            matching_assets = [
                item
                for item in wallet_assets
                if item.get("address")
                and chain
                and symbol
                and canonical_chain(str(item.get("chain"))) == canonical_chain(str(chain))
                and canonical_symbol(str(item.get("symbol"))) == canonical_symbol(str(symbol))
            ]
            unique_assets = {
                (str(item.get("address")).lower(), item.get("decimals")): item
                for item in matching_assets
            }
            if len(unique_assets) == 1:
                asset = next(iter(unique_assets.values()))
                asset_chain = asset.get("chain") or chain
                asset_symbol = asset.get("symbol") or symbol
                suggestions.append(
                    {
                        "label": (
                            f"使用钱包中的 {asset_chain} {asset_symbol}"
                            if language == "zh"
                            else f"Use wallet {asset_chain} {asset_symbol}"
                        ),
                        "message": (
                            "使用钱包余额中的 Token 合约和精度。"
                            if language == "zh"
                            else "Use the token contract and precision from the wallet balance."
                        ),
                        "data": {
                            **base_data,
                            "chain": asset_chain,
                            "symbol": asset_symbol,
                            "token_address": asset.get("address"),
                            "decimals": asset.get("decimals"),
                        },
                    }
                )

        if "transfer_chain" in missing:
            for supported_chain in facts.get("supported_chains", []):
                if len(suggestions) >= 3:
                    break
                suggestions.append(
                    {
                        "label": (
                            f"使用 {supported_chain} 网络"
                            if language == "zh"
                            else f"Use {supported_chain}"
                        ),
                        "message": (
                            f"在 {supported_chain} 网络上继续这笔转账。"
                            if language == "zh"
                            else f"Continue this transfer on {supported_chain}."
                        ),
                        "data": {**base_data, "chain": supported_chain},
                    }
                )

        return _sanitize_response_suggestions(
            suggestions, intent="transfer", facts=facts
        )

    async def naturalize_read_response(
        state: Mapping[str, Any], response: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Turn verified read facts into user-facing language at one boundary."""
        started = perf_counter()
        result = _dump(response)
        if not isinstance(result, dict):
            return dict(response)
        kind = str(result.get("kind") or "")
        if kind not in {
            "wallet_query",
            "portfolio_query",
            "price_query",
            "asset_value",
            "gas_check",
            "asset_discovery",
            "market_details",
            "price_history",
            "transaction_status",
            "swap_status",
            "swap_quote",
            "market_details",
            "price_history",
        }:
            return dict(response)
        request = dict(state.get("request") or {})
        message = str(request.get("message") or state.get("message") or "")
        facts: dict[str, Any] = {
            "intent": kind,
            "missing_fields": [],
            "user_message": message,
            "language_hint": _response_language(message, state.get("conversation_history")),
            "dialogue": _dump(state.get("dialogue") or {}),
            "result": result,
        }
        if kind == "wallet_query":
            wallet = result.get("wallet") or {}
            facts["wallet_result"] = wallet
            facts["balances"] = [
                {
                    "chain": (item.get("asset") or {}).get("chain"),
                    "symbol": (item.get("asset") or {}).get("symbol"),
                    "amount": item.get("amount"),
                    "usd_value": item.get("usd_value"),
                }
                for item in _wallet_balance_items(wallet)
            ]
            if isinstance(result.get("asset_scope"), Mapping):
                facts["asset_scope"] = dict(result["asset_scope"])
            if result.get("asset_count") is not None:
                facts["asset_count"] = result.get("asset_count")
        if kind == "portfolio_query":
            portfolio = result.get("portfolio") or result
            wallet = portfolio.get("wallet") or {}
            assets = portfolio.get("assets") or _wallet_balance_items(wallet)
            facts["portfolio_result"] = portfolio
            facts["wallet_result"] = wallet
            facts["balances"] = [
                {
                    "chain": (item.get("asset") or {}).get("chain"),
                    "symbol": (item.get("asset") or {}).get("symbol"),
                    "amount": item.get("amount"),
                    "usd_value": item.get("usd_value"),
                }
                for item in assets
                if isinstance(item, Mapping)
            ]
            facts["asset_count"] = len(assets)
            facts["total_usd_value"] = portfolio.get("total_usd_value")
        if kind == "price_query":
            facts["prices"] = result.get("prices") or []
        if kind == "asset_value":
            facts["asset_value"] = {
                "asset": result.get("asset"),
                "balance": result.get("balance"),
                "price": result.get("price"),
                "usd_value": result.get("usd_value"),
            }
        if kind == "transaction_status":
            facts["transaction"] = result
        responder = getattr(runtime.model, "respond", None)
        response_validation = "deterministic"
        if callable(responder):
            try:
                parameters = inspect.signature(responder).parameters.values()
                supports_messages = any(
                    parameter.name == "messages"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
                if supports_messages:
                    draft = _dump(
                        await responder(
                            request,
                            facts,
                            messages=list(state.get("react_messages") or []),
                        )
                    ) or {}
                else:
                    draft = _dump(await responder(request, facts)) or {}
                validated = validate_response_draft(draft, facts)
                if isinstance(validated, Mapping) and str(validated.get("message") or "").strip():
                    language = str(validated.get("language") or facts["language_hint"])
                    if language.split("-")[0].lower() == facts["language_hint"]:
                        result["message"] = str(validated["message"]).strip()
                        result["suggestions"] = _sanitize_response_suggestions(
                            validated.get("suggestions"), intent=kind, facts=facts
                        )
                        response_validation = "accepted"
                else:
                    response_validation = "rejected"
            except Exception:
                response_validation = "fallback"
        if not str(result.get("message") or "").strip():
            result["message"] = _read_response_fallback(result, facts["language_hint"])
            if response_validation == "deterministic":
                response_validation = "fallback"
        portfolio = result.get("portfolio") or result if kind == "portfolio_query" else {}
        scope = portfolio.get("scope") or {} if isinstance(portfolio, Mapping) else {}
        wallet_event(
            "response_generation",
            "success",
            duration_ms=(perf_counter() - started) * 1000,
            state=state,
            query_scope=(str(scope.get("requested")) if scope.get("requested") else None),
            resolved_chain_count=(
                len(scope.get("resolved_chains") or [])
                if isinstance(scope, Mapping)
                else None
            ),
            asset_count=(
                int(portfolio.get("asset_count"))
                if isinstance(portfolio, Mapping) and portfolio.get("asset_count") is not None
                else None
            ),
            response_validation=response_validation,
        )
        return result

    async def wallet_balance_tool(
        chain: str,
        address: str,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Read native and token balances for a connected wallet."""
        if runtime.wallet_provider is not None and hasattr(
            runtime.wallet_provider, "get_token_balances"
        ):
            normalized_chain = canonical_chain(chain)
            chain_index_by_name = dict(
                getattr(runtime.wallet_provider, "chain_index_by_name", {}) or {}
            )
            if normalized_chain in {"ALL", ""}:
                indexes = list(
                    dict.fromkeys(str(value) for value in chain_index_by_name.values())
                )
            else:
                index = chain_index_by_name.get(normalized_chain) or okx_chain_index(
                    normalized_chain
                )
                indexes = [str(index)] if index else []
            if not indexes:
                return {
                    "ok": False,
                    "error": f"当前暂不支持 {chain or '该'} 链的余额查询。",
                    "code": "OKX_CHAIN_UNSUPPORTED",
                }
            try:
                balances = await runtime.wallet_provider.get_token_balances(address, indexes)
                assets = [_dump(item) for item in balances]
                requested_symbol = canonical_symbol(str(symbol or ""))
                assets = _filter_balance_assets(
                    assets,
                    requested_symbol or None,
                )
                native = next(
                    (
                        item
                        for item in assets
                        if not (item.get("asset") or {}).get("address")
                    ),
                    None,
                )
                reverse_names = {
                    str(index): str(name).upper()
                    for name, index in chain_index_by_name.items()
                }
                return {
                    "ok": True,
                    "wallet": {
                        "address": address,
                        "chain": (
                            reverse_names.get(indexes[0], indexes[0])
                            if len(indexes) == 1
                            else "ALL"
                        ),
                        "native_balance": native,
                        "token_balances": [
                            item for item in assets if item is not native
                        ],
                        "source": "okx",
                    },
                    "asset_scope": ({"symbol": requested_symbol} if requested_symbol else None),
                    "asset_count": len(assets),
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc), "code": "WALLET_QUERY_FAILED"}
        return {
            "ok": False,
            "error": "OKX 钱包数据服务暂不可用。",
            "code": "OKX_CAPABILITY_UNAVAILABLE",
        }

    wallet_tool_node = ToolNode([wallet_balance_tool], name="wallet_tools")

    def local_transaction_status(
        chain: str,
        tx_hash: str,
        broadcast_status: str | None,
    ) -> dict[str, Any] | None:
        if not broadcast_status:
            return None
        status = {
            "not_propagated": "pending",
            "broadcast_seen": "pending",
            "broadcast_pending": "pending",
            "confirmed": "confirmed",
            "failed": "failed",
            "dropped_or_replaced": "failed",
            "unknown": "unknown",
        }.get(broadcast_status)
        if status is None:
            return None
        message = {
            "not_propagated": "交易哈希暂时还没有在源链上出现，请稍后重试。",
            "broadcast_seen": "源链已收到交易广播，正在等待链上确认。",
            "broadcast_pending": "交易已广播，正在等待链上确认。",
            "confirmed": "交易已确认。",
            "failed": "交易执行失败或已回滚。",
            "dropped_or_replaced": "交易可能已被丢弃或替换，请检查钱包 nonce。",
            "unknown": "暂时无法确定交易状态。",
        }[broadcast_status]
        return {
            "chain": str(chain).upper(),
            "tx_hash": tx_hash,
            "status": status,
            "broadcast_status": broadcast_status,
            "source": "local",
            "message": message,
        }

    async def transaction_status_tool(
        chain: str,
        tx_hash: str,
        broadcast_status: str | None = None,
    ) -> dict[str, Any]:
        """Read a transaction status from the configured chain adapter."""
        local = local_transaction_status(chain, tx_hash, broadcast_status)
        explorer = runtime.explorer_provider
        if explorer is not None and hasattr(explorer, "get_transaction_detail"):
            try:
                detail = await explorer.get_transaction_detail(chain, tx_hash)
                value = getattr(detail.status, "value", str(detail.status))
                if value == "unknown" and local is not None:
                    return {"ok": True, "transaction": local}
                return {
                    "ok": True,
                    "transaction": {
                        "chain": str(chain).upper(),
                        "tx_hash": tx_hash,
                        "status": value,
                        "message": {
                            "confirmed": "交易已确认。",
                            "pending": "交易已提交，正在等待链上确认。",
                            "failed": "交易执行失败或已回滚。",
                        }.get(value, "暂时无法确定交易状态。"),
                        "source": "okx",
                        "detail": _dump(detail),
                    },
                }
            except Exception as exc:
                if local is not None:
                    return {"ok": True, "transaction": local}
                return {"ok": False, "code": "TRANSACTION_STATUS_FAILED", "error": str(exc)}
        if local is not None:
            return {"ok": True, "transaction": local}
        return {
            "ok": False,
            "code": "OKX_CAPABILITY_UNAVAILABLE",
            "error": "OKX 交易查询服务暂不可用。",
        }

    async def gas_estimate_tool(
        chain: str,
        address: str,
        to: str | None = None,
        data: str | None = None,
    ) -> dict[str, Any]:
        """Read a network fee estimate without signing or broadcasting."""
        adapter = execution_adapter(chain)
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
            try:
                fee = await adapter.estimate_fee(to=to, data=data, from_address=address)
            except TypeError:
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
        cache = runtime.metadata_cache
        cache_key = json.dumps(
            {
                "chain": query.chain,
                "search": query.search,
                "providers": sorted(str(name) for name in selected),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if cache is not None:
            cached_assets = cache.get(cache_key)
            if cached_assets is not None:
                return {
                    "ok": True,
                    "query": query.model_dump(mode="json"),
                    "assets": cached_assets,
                    "providers": list(selected),
                    "provider_errors": [],
                    "cache_status": "hit",
                }
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
        serialized_assets = [_dump(asset) for asset in merged.values()]
        if cache is not None and not provider_errors:
            cache.set(cache_key, serialized_assets)
        return {
            "ok": True,
            "query": query.model_dump(mode="json"),
            "assets": serialized_assets,
            "providers": list(selected),
            "provider_errors": provider_errors,
            "cache_status": "miss" if cache is not None else None,
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
            raw_request = dict(swap_request)
            output_amount = raw_request.pop("output_amount", None)
            amount_mode = raw_request.pop("amount_mode", None)
            request_model = _request(raw_request)
            if amount_mode == "exact_out" and output_amount is not None:
                reverse = getattr(provider, "reverse_quote", None)
                if not callable(reverse):
                    return {
                        "ok": False,
                        "code": "REVERSE_QUOTE_UNAVAILABLE",
                        "error": f"Provider {provider_name} does not support exact-output quotes.",
                        "provider": str(provider_name),
                    }
                quote = await reverse(request_model, Decimal(str(output_amount)))
            else:
                quote = await provider.quote(request_model)
            return {"ok": True, "quote": _dump(quote)}
        except Exception as exc:
            retryable = bool(getattr(exc, "retryable", False))
            category = getattr(exc, "category", None)
            provider_code = getattr(exc, "code", None)
            return {
                "ok": False,
                "code": "PROVIDER_QUOTE_FAILED",
                "error": str(exc),
                "provider": str(provider_name),
                "provider_code": str(provider_code) if provider_code else None,
                "retryable": retryable,
                "category": category,
            }

    transaction_tool_node = ToolNode([transaction_status_tool], name="transaction_tools")
    gas_tool_node = ToolNode([gas_estimate_tool], name="gas_tools")
    asset_tool_node = ToolNode([asset_discovery_tool], name="asset_tools")
    quote_tool_node = ToolNode([quote_lookup_tool], name="quote_tools")

    async def dialogue_manager(state: dict[str, Any]) -> dict[str, Any]:
        """Project conversational context before capability routing."""
        return {
            "dialogue": dialogue_snapshot(state),
            "graph_role": "lifecycle",
            "route_mode": "lifecycle",
            "route_reason": "explicit_lifecycle_input",
        }

    async def read_only_agent(state: dict[str, Any]) -> dict[str, Any]:
        started = perf_counter()
        intent = str(state.get("intent") or "")
        request = dict(state.get("request") or {})
        request["dialogue"] = state.get("dialogue")
        request["read_facts"] = state.get("read_facts") or {}
        request["read_loop_count"] = state.get("read_loop_count", 0)
        request["task_plan"] = {
            "goal": intent,
            "completed_tools": [
                item.get("name")
                for item in state.get("read_tool_calls") or []
                if item.get("status") == "completed"
            ],
            "available_facts": sorted((state.get("read_facts") or {}).keys()),
            "max_steps": 4,
            "completed_tool_signatures": [
                str(
                    item.get("signature")
                    or read_tool_signature(str(item.get("name") or ""), item.get("args"))
                )
                for item in state.get("read_tool_calls") or []
                if item.get("status") in {"completed", "reused"}
            ],
            "max_parallel_tools": READ_ONLY_MAX_PARALLEL_TOOLS,
        }
        request_message = str(request.get("message") or "")
        other_chain_scope = _looks_like_other_chain_balance_request(request_message)
        supported_chain_names = {
            str(value).upper()
            for value in (
                set(getattr(runtime.wallet_provider, "chain_index_by_name", {}) or {})
                | set(runtime.chains)
            )
        }
        explicit_other_chain = _explicit_balance_chain(
            request_message, supported_chain_names
        )
        requested_balance_symbol = _requested_balance_symbol(
            request_message, supported_chain_names
        )
        mentioned_chain = _mentioned_balance_chain(request_message)
        if (
            intent in {"wallet_query", "portfolio_query"}
            and mentioned_chain
            and mentioned_chain not in supported_chain_names
        ):
            supported_chains = sorted(supported_chain_names)
            message = (
                f"当前暂不支持 {mentioned_chain} 链的余额查询。"
                f"可查询的网络：{'、'.join(supported_chains) or '暂无'}。你想查询哪条链？"
            )
            return {
                "response": {
                    "kind": "clarification",
                    "message": message,
                    "missing_fields": ["wallet_chain"],
                    "supported_chains": supported_chains,
                    "errors": [
                        _error(
                            "OKX_CHAIN_UNSUPPORTED",
                            message,
                            details={
                                "requested_chain": mentioned_chain,
                                "supported_chains": supported_chains,
                            },
                        )
                    ],
                },
                "read_tool_call": None,
                "read_only_complete": True,
                "read_only_pending": False,
            }
        if (
            intent in {"wallet_query", "portfolio_query"}
            and other_chain_scope
            and not explicit_other_chain
        ):
            supported_chains = sorted(supported_chain_names)
            message = (
                f"请指定要查询的网络。当前支持：{'、'.join(supported_chains) or '暂无'}。"
            )
            return {
                "response": {
                    "kind": "clarification",
                    "message": message,
                    "missing_fields": ["wallet_chain"],
                    "supported_chains": supported_chains,
                    "errors": [
                        _error(
                            "WALLET_CHAIN_REQUIRED",
                            "查询其他链余额时必须明确指定网络。",
                            details={"supported_chains": supported_chains},
                        )
                    ],
                },
                "read_tool_call": None,
                "read_only_complete": True,
                "read_only_pending": False,
            }
        native_invoker = getattr(runtime.model, "invoke_read_tools", None)
        if intent in {"wallet_query", "portfolio_query"} and not callable(native_invoker):
            if "portfolio_snapshot" in (state.get("read_facts") or {}):
                return {
                    "read_tool_call": None,
                    "read_only_complete": True,
                    "read_only_pending": False,
                }
            address = (state.get("wallet_context") or {}).get("address") or request.get(
                "address"
            )
            if address:
                arguments = {
                    "address": str(address),
                    "symbol": requested_balance_symbol,
                }
                if explicit_other_chain:
                    arguments["chain"] = explicit_other_chain
                return {
                    "read_tool_call": {
                        "name": "portfolio_snapshot",
                        "arguments": {
                            key: value for key, value in arguments.items() if value
                        },
                    },
                    "read_only_complete": False,
                    "read_only_pending": True,
                }
        if callable(native_invoker):
            existing_react_messages = list(state.get("react_messages") or [])
            messages = existing_react_messages or list(state.get("read_messages") or [])
            if not messages:
                context = {
                    "message": request.get("message"),
                    "intent": intent,
                    "wallet_context": state.get("wallet_context"),
                    "conversation_history": _model_conversation_history(
                        state.get("conversation_history")
                    ),
                    "dialogue": state.get("dialogue"),
                    "read_facts": state.get("read_facts") or {},
                    "task_plan": request.get("task_plan"),
                }
                messages = [
                    SystemMessage(
                        content=(
                            "你是钱包只读查询 Agent。只能调用提供的只读工具，"
                            "不能转账、兑换、授权、签名或广播。"
                            "每轮根据已验证的 ToolMessage 决定是否需要下一次查询；"
                            "缺少用户必须确认的网络、地址或资产时不要猜测，"
                            "直接结束并让上层提问。工具参数必须来自用户请求、钱包上下文或已验证事实。"
                        )
                    ),
                    HumanMessage(
                        content=json.dumps(context, ensure_ascii=False, default=str)
                    ),
                ]

            def react_update(*new_messages: Any) -> list[Any]:
                """Append only new messages, except when migrating a legacy checkpoint."""
                if existing_react_messages:
                    return [*new_messages]
                return [*messages, *new_messages]
            try:
                native_output = await native_invoker(messages, READ_ONLY_TOOL_SCHEMAS)
            except ValueError:
                # Injected legacy/fake models may expose the facade without a
                # tool-capable backend. Fall back to the structured planner.
                native_output = None
            if native_output is not None:
                tool_calls = list(getattr(native_output, "tool_calls", None) or [])
                invalid_calls = [
                    item
                    for item in tool_calls
                    if not isinstance(item, Mapping)
                    or str(item.get("name") or "") not in READ_ONLY_TOOL_NAMES
                    or not isinstance(item.get("args", item.get("arguments", {})), Mapping)
                ]
                if len(tool_calls) > READ_ONLY_MAX_PARALLEL_TOOLS:
                    return {
                        "response": {
                            "kind": "error",
                            "errors": [
                                _error(
                                    "READ_TOOL_BATCH_TOO_LARGE",
                                    (
                                        f"一次最多只能并行查询 {READ_ONLY_MAX_PARALLEL_TOOLS} "
                                        "个只读工具。"
                                    ),
                                    details={"max_parallel_tools": READ_ONLY_MAX_PARALLEL_TOOLS},
                                )
                            ],
                        },
                        "react_messages": react_update(native_output),
                        "read_tool_calls": [
                            dict(item) for item in state.get("read_tool_calls") or []
                        ],
                        "read_tool_call": None,
                        "read_only_complete": True,
                        "read_only_pending": False,
                        "read_native": True,
                    }
                if invalid_calls:
                    return {
                        "response": {
                            "kind": "error",
                            "errors": [
                                _error(
                                    "READ_TOOL_NOT_ALLOWED",
                                    "模型请求了不允许的只读工具或参数。",
                                )
                            ],
                        },
                        "react_messages": react_update(native_output),
                        "read_tool_call": None,
                        "read_only_complete": True,
                        "read_only_pending": False,
                        "read_native": True,
                    }
                if tool_calls:
                    existing_calls = [dict(item) for item in state.get("read_tool_calls") or []]
                    completed_signatures = {
                        str(
                            item.get("signature")
                            or read_tool_signature(
                                str(item.get("name") or ""), item.get("args")
                            )
                        )
                        for item in existing_calls
                        if item.get("status") in {"completed", "reused"}
                    }
                    incoming_signatures = [
                        read_tool_signature(
                            str(item.get("name") or ""),
                            dict(item.get("args", item.get("arguments", {}))),
                        )
                        for item in tool_calls
                    ]
                    if incoming_signatures and all(
                        signature in completed_signatures for signature in incoming_signatures
                    ):
                        reused_records = [
                            {
                                "call_id": str(item.get("id") or ""),
                                "name": str(item.get("name")),
                                "args": dict(item.get("args", item.get("arguments", {}))),
                                "signature": signature,
                                "status": "reused",
                                "elapsed_ms": 0,
                            }
                            for item, signature in zip(tool_calls, incoming_signatures, strict=True)
                        ]
                        reused_update: dict[str, Any] = {
                            "react_messages": react_update(native_output),
                            "read_tool_calls": [*existing_calls, *reused_records],
                            "read_tool_call": None,
                            "read_only_complete": True,
                            "read_only_pending": False,
                            "read_native": True,
                        }
                        if isinstance(state.get("react_result"), Mapping):
                            reused_update["response"] = await naturalize_read_response(
                                {
                                    **state,
                                    "react_messages": [*messages, native_output],
                                },
                                state["react_result"],
                            )
                        return reused_update
                    records = [
                        {
                            "call_id": str(item.get("id") or ""),
                            "name": str(item.get("name")),
                            "args": dict(item.get("args", item.get("arguments", {}))),
                            "signature": signature,
                            "status": "started",
                        }
                        for item, signature in zip(tool_calls, incoming_signatures, strict=True)
                    ]
                    return {
                        "react_messages": react_update(native_output),
                        "read_tool_call": {"native": True},
                        "read_tool_calls": [*existing_calls, *records],
                        "read_only_complete": False,
                        "read_only_pending": True,
                        "read_native": True,
                    }
                completed: dict[str, Any] = {
                    "react_messages": react_update(native_output),
                    "read_tool_call": None,
                    "read_only_complete": True,
                    "read_only_pending": False,
                    "read_native": True,
                }
                if isinstance(state.get("react_result"), Mapping):
                    completed["response"] = await naturalize_read_response(
                        {**state, "react_messages": [*messages, native_output]},
                        state["react_result"],
                    )
                return completed
        call = await read_only_decision(runtime.model, request)
        if _looks_like_asset_value_query(str(request.get("message") or "")):
            context = state.get("wallet_context") or {}
            address = context.get("address") or request.get("address")
            chain = context.get("chain") or request.get("chain")
            if address and chain and "wallet_balance" not in state.get("read_facts", {}):
                # The estimate is a deterministic fan-in and always needs a
                # balance.  Ask for it even when the planner selected price first.
                call = ReadOnlyToolCall(
                    name="wallet_balance",
                    arguments={"address": str(address), "chain": str(chain)},
                )
            elif (
                "wallet_balance" in state.get("read_facts", {})
                and "price_lookup" not in state.get("read_facts", {})
            ):
                raw_price = state.get("price_request") or {}
                if raw_price.get("symbol"):
                    call = ReadOnlyToolCall(
                        name="price_lookup",
                        arguments={
                            key: value
                            for key, value in {
                                "symbol": raw_price.get("symbol"),
                                "chain": raw_price.get("chain") or chain,
                                "token_address": (
                                    raw_price.get("address")
                                    or raw_price.get("token_address")
                                ),
                                "decimals": raw_price.get("decimals"),
                            }.items()
                            if value not in (None, "")
                        },
                    )
        if call is None:
            if state.get("read_loop_count", 0) == 0:
                name = read_only_tool_name_for_intent(intent)
                if name == "wallet_balance":
                    context = state.get("wallet_context") or {}
                    address = context.get("address") or request.get("address")
                    chain = context.get("chain") or request.get("chain")
                    if address and chain:
                        call = ReadOnlyToolCall(
                            name=name,
                            arguments={"address": str(address), "chain": str(chain)},
                        )
                elif name == "transaction_status":
                    raw = state.get("transaction_query") or {}
                    call = ReadOnlyToolCall(
                        name=name,
                        arguments={
                            "chain": raw.get("chain") or raw.get("transaction_chain"),
                            "tx_hash": raw.get("tx_hash") or raw.get("transaction_hash"),
                        },
                    )
            if call is None:
                update = {
                    "read_tool_call": None,
                    "read_only_complete": True,
                    "read_only_pending": False,
                }
                wallet_event(
                    "read_only_agent",
                    "success",
                    duration_ms=(perf_counter() - started) * 1000,
                    state=state,
                )
                return update
        update = {
            "read_tool_call": {
                "name": call.name,
                "arguments": call.arguments,
            },
            "read_only_complete": False,
            "read_only_pending": True,
        }
        wallet_event(
            "read_only_agent",
            "success",
            duration_ms=(perf_counter() - started) * 1000,
            state=state,
            tool_name=call.name,
            tool_status="planned",
        )
        return update

    async def read_only_tool(state: dict[str, Any]) -> dict[str, Any]:
        started = perf_counter()
        raw_call = state.get("read_tool_call") or {}
        name = str(raw_call.get("name") or "")
        arguments = raw_call.get("arguments") or {}
        request_message = str((state.get("request") or {}).get("message") or "")
        supported_chain_names = {
            str(value).upper()
            for value in (
                set(getattr(runtime.wallet_provider, "chain_index_by_name", {}) or {})
                | set(runtime.chains)
            )
        }
        requested_balance_symbol = _requested_balance_symbol(
            request_message, supported_chain_names
        )

        async def price_lookup(args: dict[str, Any]) -> dict[str, Any]:
            """Read the current price for an asset."""
            raw = state.get("price_request") or {}
            merged = {**raw, **args}
            asset, error = _normalize_price_asset(
                merged, {}, state.get("wallet_context")
            )
            if error and error.get("code") == "PRICE_ASSET_UNRESOLVED" and merged.get("chain"):
                discovered, candidates = await _resolve_asset_metadata(
                    merged, runtime.providers, state.get("wallet_context")
                )
                if discovered is not None:
                    asset, error = discovered, None
                elif candidates:
                    error = {
                        "code": "PRICE_ASSET_AMBIGUOUS",
                        "error": (
                            f"{merged.get('symbol')} 在 {merged.get('chain')} 上有多个同名资产，"
                            "请选择要使用的资产。"
                        ),
                        "symbol": merged.get("symbol"),
                        "chain": merged.get("chain"),
                        "candidates": candidates,
                    }
            if error:
                return {"ok": False, **error}
            if runtime.price_provider is None:
                return {
                    "ok": False,
                    "code": "PRICE_PROVIDER_UNAVAILABLE",
                    "error": "价格服务暂不可用。",
                }
            prices = await runtime.price_provider.get_prices([asset])
            return {"ok": True, "prices": [_dump(item) for item in prices]}

        async def wallet_balance(args: dict[str, Any]) -> dict[str, Any]:
            """Read wallet balances."""
            context = state.get("wallet_context") or {}
            chain = args.get("chain") or context.get("chain")
            address = args.get("address") or context.get("address")
            if not chain or not address:
                return {
                    "ok": False,
                    "code": "WALLET_CONTEXT_REQUIRED",
                    "error": "请先连接钱包并明确查询网络。",
                }
            requested_symbol = canonical_symbol(str(args.get("symbol") or ""))
            return await wallet_balance_tool(
                str(chain), str(address), requested_symbol or None
            )

        async def transaction_status(args: dict[str, Any]) -> dict[str, Any]:
            """Read a transaction status."""
            context = state.get("wallet_context") or {}
            return await transaction_status_tool(
                str(args.get("chain") or context.get("chain") or ""),
                str(args.get("tx_hash") or ""),
                state.get("broadcast_status"),
            )

        async def gas_estimate(args: dict[str, Any]) -> dict[str, Any]:
            """Estimate a network fee."""
            context = state.get("wallet_context") or {}
            chain = args.get("chain") or context.get("chain")
            address = args.get("address") or context.get("address")
            if not chain or not address:
                return {
                    "ok": False,
                    "code": "WALLET_CONTEXT_REQUIRED",
                    "error": "请先连接钱包并明确查询网络。",
                }
            return await gas_estimate_tool(
                str(chain),
                str(address),
                args.get("to"),
                args.get("data"),
            )

        async def asset_discovery(args: dict[str, Any]) -> dict[str, Any]:
            """Discover token metadata."""
            return await asset_discovery_tool(
                args.get("chain"), args.get("search"), args.get("provider_name")
            )

        async def market_details(args: dict[str, Any]) -> dict[str, Any]:
            """Read market details for an asset."""
            raw = {**(state.get("price_request") or {}), **args}
            asset, error = _normalize_price_asset(raw, {}, state.get("wallet_context"))
            if error:
                return {"ok": False, **error}
            provider = runtime.price_provider
            method = getattr(provider, "get_market_details", None) if provider else None
            if method is None:
                return {
                    "ok": False,
                    "code": "MARKET_DETAILS_UNAVAILABLE",
                    "error": "行情详情暂不可用。",
                }
            try:
                details = await method([asset])
                return {"ok": True, "details": [_dump(item) for item in details]}
            except Exception:
                return {"ok": False, "code": "MARKET_DETAILS_FAILED", "error": "行情详情暂不可用。"}

        async def price_history(args: dict[str, Any]) -> dict[str, Any]:
            """Read historical prices for an asset."""
            raw = {**(state.get("price_request") or {}), **args}
            asset, error = _normalize_price_asset(raw, {}, state.get("wallet_context"))
            if error:
                return {"ok": False, **error}
            provider = runtime.price_provider
            method = getattr(provider, "get_historical_prices", None) if provider else None
            if method is None:
                return {
                    "ok": False,
                    "code": "PRICE_HISTORY_UNAVAILABLE",
                    "error": "历史价格暂不可用。",
                }
            try:
                page = await method(
                    asset,
                    period=str(args.get("period") or "1h"),
                    limit=int(args.get("limit") or 24),
                )
                return {"ok": True, "history": _dump(page)}
            except Exception:
                return {"ok": False, "code": "PRICE_HISTORY_FAILED", "error": "历史价格暂不可用。"}

        async def portfolio_snapshot(args: dict[str, Any]) -> dict[str, Any]:
            """Read a wallet portfolio snapshot."""
            context = state.get("wallet_context") or {}
            address = str(args.get("address") or context.get("address") or "")
            requested_symbol = canonical_symbol(str(args.get("symbol") or ""))
            chain = canonical_chain(str(args.get("chain") or ""))
            query_all_chains = not chain
            if not address:
                return {"ok": False, "code": "WALLET_ADDRESS_REQUIRED", "error": "请先连接钱包。"}
            try:
                provider = runtime.wallet_provider
                if provider is None or not hasattr(provider, "get_token_balances"):
                    return {
                        "ok": False,
                        "code": "OKX_CAPABILITY_UNAVAILABLE",
                        "error": "OKX 钱包数据服务暂不可用。",
                    }
                chain_index_by_name = dict(
                    getattr(provider, "chain_index_by_name", {}) or {}
                )
                if query_all_chains:
                    indexes = list(
                        dict.fromkeys(str(value) for value in chain_index_by_name.values())
                    )
                    if not indexes:
                        return {
                            "ok": False,
                            "code": "OKX_CHAIN_UNSUPPORTED",
                            "error": "当前没有可用于全链资产查询的网络。",
                        }
                    reverse_names = {
                        str(index): str(name).upper()
                        for name, index in chain_index_by_name.items()
                    }
                    resolved_chains = [reverse_names.get(index, index) for index in indexes]
                    requested_scope = "all"
                else:
                    index = chain_index_by_name.get(chain) or okx_chain_index(chain)
                    if not index:
                        return {
                            "ok": False,
                            "code": "OKX_CHAIN_UNSUPPORTED",
                            "error": f"当前暂不支持 {chain} 链的余额查询。",
                        }
                    indexes = [str(index)]
                    resolved_chains = [chain] if chain else []
                    requested_scope = "specified"
                raw_balances = await provider.get_token_balances(address, indexes)
                assets = [_dump(item) for item in raw_balances]
                if runtime.price_provider is not None and raw_balances:
                    try:
                        prices = await runtime.price_provider.get_prices(
                            [balance.asset for balance in raw_balances]
                        )
                    except Exception:
                        prices = []
                    price_map = {
                        (
                            canonical_chain(str(price.asset.chain)),
                            canonical_symbol(str(price.asset.symbol)),
                            str(price.asset.address or "").lower(),
                        ): price
                        for price in prices
                    }
                    for item, balance in zip(assets, raw_balances, strict=False):
                        if item.get("usd_value") is not None:
                            continue
                        price = price_map.get(
                            (
                                canonical_chain(str(balance.asset.chain)),
                                canonical_symbol(str(balance.asset.symbol)),
                                str(balance.asset.address or "").lower(),
                            )
                        )
                        if price is not None:
                            item["usd_value"] = str(balance.amount * price.usd_price)
                assets = _filter_balance_assets(
                    assets,
                    requested_symbol or None,
                    positive_only=True,
                )
                native = next(
                    (
                        item
                        for item in assets
                        if not (item.get("asset") or {}).get("address")
                        and not query_all_chains
                    ),
                    None,
                )
                wallet: dict[str, Any] = {
                    "address": address,
                    "chain": "ALL" if query_all_chains else chain,
                    "native_balance": native,
                    "token_balances": [item for item in assets if item is not native],
                    "source": "okx",
                }
                total = None
                if hasattr(provider, "get_total_value"):
                    value = await provider.get_total_value(
                        address, [item for item in indexes if item]
                    )
                    total = _dump(value)
                known_values = [
                    Decimal(str(item["usd_value"]))
                    for item in assets
                    if item.get("usd_value") is not None
                ]
                total_usd_value = (
                    str(sum(known_values, Decimal("0")))
                    if requested_symbol and len(known_values) == len(assets)
                    else (
                        total.get("total_value")
                        if isinstance(total, Mapping)
                        else None
                    )
                )
                return {
                    "ok": True,
                    "portfolio": {
                        "wallet": wallet,
                        "assets": assets,
                        "asset_count": len(assets),
                        "chains": resolved_chains,
                        "scope": {
                            "requested": requested_scope,
                            "resolved_chains": resolved_chains,
                        },
                        "asset_scope": (
                            {"symbol": requested_symbol}
                            if requested_symbol
                            else None
                        ),
                        "total_value": total,
                        "total_usd_value": total_usd_value,
                        "complete": True,
                    },
                }
            except Exception:
                return {
                    "ok": False,
                    "code": "PORTFOLIO_QUERY_FAILED",
                    "error": "资产组合暂不可用。",
                }

        tools = {
            "wallet_balance": wallet_balance,
            "price_lookup": price_lookup,
            "gas_estimate": gas_estimate,
            "asset_discovery": asset_discovery,
            "transaction_status": transaction_status,
            "market_details": market_details,
            "price_history": price_history,
            "portfolio_snapshot": portfolio_snapshot,
        }
        async def native_price_lookup(
            symbol: str | None = None,
            chain: str | None = None,
            token_address: str | None = None,
            decimals: int | None = None,
        ) -> dict[str, Any]:
            """Read the current price for an asset."""
            return await price_lookup(
                {
                    "symbol": symbol,
                    "chain": chain,
                    "token_address": token_address,
                    "decimals": decimals,
                }
            )

        async def native_wallet_balance(
            chain: str | None = None,
            address: str | None = None,
            symbol: str | None = None,
        ) -> dict[str, Any]:
            """Read wallet balances."""
            return await wallet_balance(
                {
                    "chain": chain,
                    "address": address,
                    "symbol": symbol or requested_balance_symbol,
                }
            )

        async def native_transaction_status(
            tx_hash: str | None = None, chain: str | None = None
        ) -> dict[str, Any]:
            """Read a transaction status."""
            return await transaction_status({"tx_hash": tx_hash, "chain": chain})

        async def native_gas_estimate(
            chain: str | None = None,
            address: str | None = None,
            to: str | None = None,
            data: str | None = None,
        ) -> dict[str, Any]:
            """Estimate a network fee."""
            return await gas_estimate(
                {"chain": chain, "address": address, "to": to, "data": data}
            )

        async def native_asset_discovery(
            chain: str | None = None,
            search: str | None = None,
            provider_name: str | None = None,
        ) -> dict[str, Any]:
            """Discover token metadata."""
            return await asset_discovery(
                {"chain": chain, "search": search, "provider_name": provider_name}
            )

        async def native_market_details(
            symbol: str | None = None,
            chain: str | None = None,
            token_address: str | None = None,
            decimals: int | None = None,
        ) -> dict[str, Any]:
            """Read market details for an asset."""
            return await market_details(
                {
                    "symbol": symbol,
                    "chain": chain,
                    "token_address": token_address,
                    "decimals": decimals,
                }
            )

        async def native_price_history(
            symbol: str | None = None,
            chain: str | None = None,
            token_address: str | None = None,
            decimals: int | None = None,
            period: str | None = None,
            limit: int | None = None,
        ) -> dict[str, Any]:
            """Read historical prices for an asset."""
            return await price_history(
                {
                    "symbol": symbol,
                    "chain": chain,
                    "token_address": token_address,
                    "decimals": decimals,
                    "period": period,
                    "limit": limit,
                }
            )

        async def native_portfolio_snapshot(
            address: str | None = None,
            chain: str | None = None,
            symbol: str | None = None,
        ) -> dict[str, Any]:
            """Read a wallet portfolio snapshot."""
            return await portfolio_snapshot(
                {
                    "address": address,
                    "chain": chain,
                    "symbol": symbol or requested_balance_symbol,
                }
            )

        native_tools = [
            native_wallet_balance,
            native_price_lookup,
            native_gas_estimate,
            native_asset_discovery,
            native_transaction_status,
            native_market_details,
            native_price_history,
            native_portfolio_snapshot,
        ]
        for native_tool, tool_name in zip(
            native_tools, tools, strict=True
        ):
            native_tool.__name__ = tool_name
        if state.get("read_native"):
            count = int(state.get("read_loop_count") or 0) + 1
            messages = list(state.get("react_messages") or [])
            calls = [dict(item) for item in state.get("read_tool_calls") or []]
            facts = dict(state.get("read_facts") or {})
            try:
                tool_result = await ToolNode(native_tools, name="read_only_tools").ainvoke(
                    {"messages": [messages[-1]]}
                )
                tool_messages = list(tool_result.get("messages") or [])
            except Exception as exc:
                for record in calls:
                    if record.get("status") == "started":
                        record.update(
                            status="failed",
                            error={"code": "READ_TOOL_FAILED", "message": str(exc)},
                        )
                facts["error"] = {
                    "code": "READ_TOOL_FAILED",
                    "message": str(exc),
                }
                return {
                    "read_loop_count": count,
                    "react_messages": [],
                    "read_tool_calls": calls,
                    "read_facts": facts,
                    "read_tool_call": None,
                    "read_only_pending": False,
                    "read_only_complete": True,
                }
            for message in tool_messages:
                call_id = str(getattr(message, "tool_call_id", "") or "")
                name = str(getattr(message, "name", "") or "")
                record = next(
                    (item for item in calls if str(item.get("call_id")) == call_id),
                    None,
                )
                if record is not None:
                    name = name or str(record.get("name") or "")
                content = getattr(message, "content", message)
                try:
                    payload = content if isinstance(content, dict) else json.loads(str(content))
                except (TypeError, ValueError):
                    payload = {
                        "ok": False,
                        "code": "TOOL_OUTPUT_INVALID",
                        "error": str(content),
                    }
                if not isinstance(payload, dict):
                    payload = {
                        "ok": False,
                        "code": "TOOL_OUTPUT_INVALID",
                        "error": "只读工具返回了非 JSON 对象。",
                    }
                facts[name] = payload
                if record is not None:
                    record.update(
                        status="completed" if payload.get("ok", True) else "failed",
                        elapsed_ms=round((perf_counter() - started) * 1000, 2),
                    )
                    if not payload.get("ok", True):
                        record["error"] = {
                            "code": payload.get("code", "READ_TOOL_FAILED"),
                            "message": payload.get("error", "只读工具调用失败。"),
                        }
            wallet_event(
                "read_only_tool",
                "success" if all(item.get("status") == "completed" for item in calls) else "error",
                duration_ms=(perf_counter() - started) * 1000,
                state={**state, "read_facts": facts, "read_tool_calls": calls},
                tool_name="native",
                tool_status="completed",
            )
            return {
                "read_loop_count": count,
                "react_messages": tool_messages,
                "read_tool_calls": calls,
                "read_facts": facts,
                "read_tool_call": None,
                "read_only_pending": True,
                "read_only_complete": False,
            }
        update = await run_read_only_round(
            state,
            ReadOnlyToolCall(name=name, arguments=dict(arguments)),
            tools,
        )
        record = (update.get("read_tool_calls") or [{}])[-1]
        wallet_event(
            "read_only_tool",
            "error" if record.get("status") in {"failed", "rejected"} else "success",
            duration_ms=(perf_counter() - started) * 1000,
            state={**state, **update},
            error_code=(record.get("error") or {}).get("code"),
            tool_name=name,
            tool_status=record.get("status"),
        )
        return update

    async def read_only_observe(state: dict[str, Any]) -> dict[str, Any]:
        started = perf_counter()
        facts = state.get("read_facts") or {}
        result: dict[str, Any] = {}
        request = _mapping(state.get("request"))
        message = str(request.get("message") or state.get("message") or "")
        language = _response_language(message, state.get("conversation_history"))
        # A value request is a fan-in operation.  Do this before the generic
        # last-tool projection so a failed price call cannot hide a successful
        # balance call (or vice versa).
        if _looks_like_asset_value_query(message):
            price_request = _mapping(state.get("price_request"))
            price_calls = [
                item
                for item in state.get("read_tool_calls") or []
                if str(item.get("name") or "") == "price_lookup"
            ]
            latest_price_args = _mapping(price_calls[-1].get("args")) if price_calls else {}
            requested_symbol = str(
                latest_price_args.get("symbol")
                or price_request.get("symbol")
                or ""
            ) or None
            requested_chain = str(
                latest_price_args.get("chain")
                or price_request.get("chain")
                or ""
            ) or None
            aggregated = _asset_value_response(
                facts,
                language,
                requested_symbol=requested_symbol,
                requested_chain=requested_chain,
            )
            if aggregated is not None:
                result = aggregated
        call_name = str((state.get("read_tool_calls") or [{}])[-1].get("name") or "")
        latest = facts.get(call_name) if call_name else None
        if result:
            pass
        elif call_name == "wallet_balance" and isinstance(latest, dict) and latest.get("ok"):
            result = {"kind": "wallet_query", "wallet": latest.get("wallet") or {}}
        elif call_name == "price_lookup" and isinstance(latest, dict) and latest.get("ok"):
            result = {"kind": "price_query", "prices": latest.get("prices") or []}
        elif call_name == "transaction_status" and isinstance(latest, dict) and latest.get("ok"):
            result = {"kind": "transaction_status", **(latest.get("transaction") or {})}
        elif call_name == "gas_estimate" and isinstance(latest, dict) and latest.get("ok"):
            result = {"kind": "gas_check", **(latest.get("gas") or {})}
        elif call_name == "asset_discovery" and isinstance(latest, dict) and latest.get("ok"):
            result = {"kind": "asset_discovery", **latest}
        elif call_name == "market_details" and isinstance(latest, dict) and latest.get("ok"):
            result = {"kind": "market_details", **latest}
        elif call_name == "price_history" and isinstance(latest, dict) and latest.get("ok"):
            result = {"kind": "price_history", **latest}
        elif call_name == "portfolio_snapshot" and isinstance(latest, dict) and latest.get("ok"):
            portfolio = latest.get("portfolio") or {}
            asset_scope = portfolio.get("asset_scope")
            if isinstance(asset_scope, Mapping) and asset_scope.get("symbol"):
                result = {
                    "kind": "wallet_query",
                    "wallet": portfolio.get("wallet") or {},
                    "assets": portfolio.get("assets") or [],
                    "asset_count": portfolio.get("asset_count"),
                    "asset_scope": dict(asset_scope),
                    "scope": portfolio.get("scope") or {},
                }
            else:
                result = {"kind": "portfolio_query", **portfolio}
        if not result:
            error = facts.get("error") or {}
            latest_error = latest if isinstance(latest, Mapping) else {}
            if latest_error.get("code") in {
                "PRICE_CHAIN_REQUIRED",
                "PRICE_ASSET_UNRESOLVED",
                "PRICE_ASSET_AMBIGUOUS",
            }:
                message = str(latest_error.get("error") or "请补充资产所属网络。")
                detail = _error(
                    str(latest_error["code"]),
                    message,
                    details={
                        key: latest_error[key]
                        for key in ("symbol", "chain")
                        if key in latest_error
                    },
                )
                return {
                    "response": {
                        "kind": "clarification",
                        "message": message,
                        "missing_fields": (
                            ["price_chain"]
                            if latest_error["code"] == "PRICE_CHAIN_REQUIRED"
                            else []
                        ),
                        "errors": [detail],
                    },
                    "read_only_complete": True,
                    "read_only_pending": False,
                }
            unsupported_chain = (
                isinstance(latest, Mapping)
                and latest.get("code") == "OKX_CHAIN_UNSUPPORTED"
            )
            if unsupported_chain:
                requested_chain = str(
                    ((state.get("read_tool_calls") or [{}])[-1].get("args") or {}).get(
                        "chain"
                    )
                    or ""
                ).upper()
                supported_chains = sorted(
                    {
                        str(value).upper()
                        for value in (
                            set(getattr(runtime.wallet_provider, "chain_index_by_name", {}) or {})
                            | set(runtime.chains)
                        )
                    }
                )
                message = (
                    f"当前暂不支持 {requested_chain or '该'} 链的余额查询。"
                    f"可查询的网络：{'、'.join(supported_chains) or '暂无'}。你想查询哪条链？"
                )
                detail = _error(
                    "OKX_CHAIN_UNSUPPORTED",
                    str(latest.get("error") or message),
                    details={
                        "requested_chain": requested_chain,
                        "supported_chains": supported_chains,
                    },
                )
                update = {
                    "response": {
                        "kind": "clarification",
                        "message": message,
                        "missing_fields": ["wallet_chain"],
                        "supported_chains": supported_chains,
                        "errors": [detail],
                    },
                    "read_only_complete": True,
                    "read_only_pending": False,
                }
                wallet_event(
                    "read_only_observe",
                    "recoverable_error",
                    duration_ms=(perf_counter() - started) * 1000,
                    state=state,
                    error_code="OKX_CHAIN_UNSUPPORTED",
                    tool_name=call_name,
                )
                return update
            update = {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            error.get("code", "READ_TOOL_FAILED"),
                            error.get("message", "只读查询失败。"),
                        )
                    ],
                },
                "read_only_complete": True,
                "read_only_pending": False,
            }
            wallet_event(
                "read_only_observe",
                "error",
                duration_ms=(perf_counter() - started) * 1000,
                state=state,
                error_code=error.get("code", "READ_TOOL_FAILED"),
                tool_name=call_name,
            )
            return update
        final = int(state.get("read_loop_count") or 0) >= READ_ONLY_MAX_ROUNDS
        if state.get("read_native") and not final:
            update = {
                "react_result": result,
                "response": None,
                "read_tool_call": None,
                "read_only_complete": False,
                "read_only_pending": True,
            }
        else:
            update = {
                "response": await naturalize_read_response(state, result),
                "read_tool_call": None,
                "read_only_complete": final,
                "read_only_pending": not final,
            }
        wallet_event(
            "read_only_observe",
            "success",
            duration_ms=(perf_counter() - started) * 1000,
            state=state,
            tool_name=call_name,
            tool_status="completed",
        )
        return update

    def route_read_only(state: dict[str, Any]) -> str:
        from .read_only import route_read_only as route

        if state.get("response") and not state.get("read_only_pending"):
            return "response"
        return route(state)

    async def supervisor(state: dict[str, Any]) -> dict[str, Any]:
        """Plan one turn; execution remains in the existing graph nodes."""
        started = perf_counter()
        _emit_progress(
            "supervisor",
            "started",
            message="正在识别请求类型。",
        )
        request = _mapping(state.get("request"))
        forced = state.get("forced_intent")
        metadata = request.get("metadata")
        suggestion_data = _suggestion_data_for_model(
            metadata.get("suggestion_data") if isinstance(metadata, Mapping) else None
        )
        message = str(request.get("message", ""))
        explicit_transfer = _looks_like_transfer_message(message)
        historical_swap = _latest_historical_swap(state.get("operation_history"))
        followup_intent = (
            _active_task_followup_intent(
                state.get("active_task"), message, suggestion_data
            )
            if hasattr(runtime.model, "extract")
            else None
        )
        history_followup_intent = _history_followup_intent(
            message, state.get("conversation_history")
        )
        preflight_followup = _latest_preflight_failure(state.get("conversation_history"))
        resolved_reference = resolve_read_reference(
            message, state.get("dialogue")
        )
        selected_quote_reference = resolve_quote_reference(
            message,
            (state.get("dialogue") or {}).get("referent"),
        )
        if _looks_like_preflight_balance_followup(
            message, state.get("conversation_history")
        ) and preflight_followup is not None:
            decision = {
                "intent": "clarification",
                "source": "preflight_followup",
                "preflight_followup": preflight_followup,
            }
        elif _looks_like_cancel_message(message) and (
            state.get("active_task")
            or state.get("conversation_state")
            or state.get("swap_draft")
            or state.get("transfer_draft")
        ):
            decision = {"intent": "clarification", "source": "command_guard"}
        elif forced in _VALID_INTENTS:
            decision = {"intent": forced, "source": "api"}
        elif state.get("intent") in _VALID_INTENTS and not request.get("message"):
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
            model_request["active_task"] = state.get("active_task")
            model_request["swap_draft"] = state.get("swap_draft")
            model_request["transfer_draft"] = state.get("transfer_draft")
            model_request["conversation_history"] = _model_conversation_history(
                state.get("conversation_history")
            )
            model_request["dialogue"] = state.get("dialogue") or dialogue_snapshot(state)
            previous_response = _latest_read_response(state.get("conversation_history"))
            if previous_response is not None:
                model_request["previous_read_response"] = previous_response
            if preflight_followup is not None:
                model_request["previous_preflight"] = preflight_followup
            model_request["routing_hints"] = {
                "explicit_transfer": explicit_transfer,
                "asset_value_query": _looks_like_asset_value_query(message),
                "active_task_followup_intent": followup_intent,
                "history_followup_intent": history_followup_intent,
                "resolved_reference": resolved_reference,
                "selected_quote_reference": selected_quote_reference,
                "historical_swap_status": (
                    historical_swap.get("task_id")
                    if historical_swap and _looks_like_swap_status_message(message)
                    else None
                ),
            }
            if suggestion_data is not None:
                model_request["suggestion_data"] = suggestion_data
            model_request["available_capabilities"] = {
                "read_tools": [
                    "wallet_balance",
                    "transaction_status",
                    "gas_estimate",
                    "asset_discovery",
                    "market_details",
                    "price_history",
                    "portfolio_snapshot",
                ],
                "business_nodes": ["transfer", "swap_allowance", "prepare", "status_poll"],
            }
            try:
                if hasattr(runtime.model, "classify"):
                    output = runtime.model.classify(model_request)
                elif hasattr(runtime.model, "ainvoke"):
                    output = runtime.model.ainvoke(model_request)
                else:
                    output = runtime.model.invoke(model_request)
                output = await output if hasattr(output, "__await__") else output
                decision = _parse_model_output(output)
            except Exception as exc:
                decision = {
                    "intent": "clarification",
                    "error": _error("SUPERVISOR_FAILED", str(exc), retryable=True),
                }
            if decision is not None:
                try:
                    # Legacy `ainvoke` models may return intent plus extracted
                    # slots. Validate the routing field strictly while
                    # preserving those slots for the existing intent node.
                    validated_route = RouteDecision.model_validate(
                        {"intent": decision.get("intent")}
                    )
                    decision = {
                        **decision,
                        "intent": validated_route.intent,
                    }
                except Exception:
                    decision = {
                        "intent": "clarification",
                        "error": _error(
                            "SUPERVISOR_OUTPUT_INVALID",
                            "无法判断这次请求应该执行哪项钱包能力。",
                        ),
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
            # Deterministic parsers are a bounded fallback for an invalid or
            # non-committal model answer. They never override a valid model
            # route for a new business request.
            decision_error = decision.get("error")
            decision_error_code = (
                decision_error.get("code")
                if isinstance(decision_error, Mapping)
                else None
            )
            if (
                decision.get("intent") in {"clarification", "unsupported"}
                and decision_error_code != "SUPERVISOR_OUTPUT_INVALID"
            ):
                fallback: dict[str, Any] | None = None
                if explicit_transfer and not _looks_like_cancel_message(message):
                    fallback = {"intent": "transfer", "source": "semantic_fallback"}
                elif _looks_like_asset_value_query(message):
                    fallback = {"intent": "price_query", "source": "semantic_fallback"}
                elif historical_swap and _looks_like_swap_status_message(message):
                    fallback = {
                        "intent": "swap_status",
                        "source": "operation_history",
                        "operation_reference_task_id": historical_swap.get("task_id"),
                    }
                elif followup_intent in _VALID_INTENTS:
                    fallback = {"intent": followup_intent, "source": "active_task"}
                elif history_followup_intent in _VALID_INTENTS:
                    fallback = {"intent": history_followup_intent, "source": "history_fallback"}
                elif resolved_reference is not None:
                    fallback = {
                        "intent": resolved_reference["intent"],
                        "source": "dialogue_reference_fallback",
                    }
                elif selected_quote_reference is not None:
                    fallback = {
                        "intent": "swap_select",
                        "source": "quote_reference_fallback",
                        "selected_quote": selected_quote_reference["arguments"]["selected_quote"],
                    }
                if fallback is not None:
                    decision = fallback
        if (
            decision.get("intent") == "transaction_status"
            and state.get("provider_orders")
            and state.get("broadcast_tx_hash")
            and not transaction_query_hints(message).get(
                "transaction_hash"
            )
        ):
            # A status follow-up inside an active swap asks about the provider
            # order unless the user explicitly supplied a different hash.
            decision = {"intent": "swap_status", "source": "swap_context"}
        update: dict[str, Any] = {
            "intent": decision["intent"],
            "predicted_intent": decision["intent"],
            "supervisor_decision": decision,
            "supervisor_output": decision,
            "response_action": None,
            # Preserve every legacy request field while making the checkpoint
            # representation serializable for subsequent resume calls.
            "request": request,
            "read_loop_count": 0,
            "read_tool_calls": [],
            "read_messages": [],
            "react_messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)],
            "react_result": None,
            "read_native": False,
            "read_facts": {},
            "read_tool_call": None,
            "read_only_complete": False,
            "read_only_pending": False,
        }
        if decision.get("source") == "preflight_followup":
            update["preflight_followup"] = decision.get("preflight_followup")
        has_native_read_tools = hasattr(runtime.model, "invoke_read_tools")
        has_structured_read_planner = hasattr(runtime.model, "plan_read")
        update["read_only_enabled"] = bool(
            getattr(
                runtime.model,
                "read_agent_enabled",
                has_native_read_tools or has_structured_read_planner,
            )
            and (has_native_read_tools or has_structured_read_planner)
            and decision.get("intent")
            in {
                "wallet_query",
                "price_query",
                "transaction_status",
                "gas_check",
                "asset_discovery",
                "market_details",
                "price_history",
                "portfolio_query",
            }
        )
        if decision.get("source") == "operation_history" and historical_swap is not None:
            update.update(
                operation_reference="history",
                operation_reference_task_id=historical_swap.get("task_id"),
                provider_orders=historical_swap.get("provider_orders") or {},
                provider_order_ids=historical_swap.get("provider_order_ids") or {},
                status_snapshot=historical_swap.get("status_snapshot"),
                broadcast_tx_hash=historical_swap.get("broadcast_tx_hash"),
                broadcast_status=historical_swap.get("broadcast_status"),
                selected_quote=historical_swap.get("selected_quote"),
            )
        if decision.get("source") == "dialogue_reference":
            update["read_reference"] = resolved_reference
        if decision.get("source") == "quote_reference":
            update["selected_quote"] = decision["selected_quote"]
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
        error = decision.get("error") or {}
        wallet_event(
            "supervisor",
            "error" if error else "success",
            duration_ms=(perf_counter() - started) * 1000,
            state={
                **state,
                "predicted_intent": decision["intent"],
                "route": decision["intent"],
            },
            error_code=error.get("code") if isinstance(error, dict) else None,
        )
        _emit_progress(
            "supervisor",
            "failed" if error else "completed",
            started=started,
            message="请求类型识别完成。" if not error else "请求类型识别失败。",
            intent=decision["intent"],
            error_code=error.get("code") if isinstance(error, dict) else None,
        )
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
        response = await naturalize_read_response(
            state,
            {"kind": "wallet_query", "wallet": wallet},
        )
        return {
            "wallet_context": wallet,
            "response": response,
        }

    async def transaction_tool_call(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("transaction_query") or {}
        chain = (
            raw.get("chain")
            or raw.get("transaction_chain")
            or state.get("request", {}).get("chain")
        )
        tx_hash = raw.get("tx_hash") or raw.get("transaction_hash")
        pending = _mapping(state.get("pending_transaction"))
        if not tx_hash and pending:
            transaction = {
                "chain": str(chain or pending.get("chain") or "").upper(),
                "status": "not_broadcast",
                "source": "local",
                "message": "交易尚未广播，请先在钱包中签名并广播。",
            }
            return {"tool_result": {"ok": True, "transaction": transaction}}
        if not chain or not tx_hash:
            return {
                "tool_result": {
                    "ok": False,
                    "code": "TRANSACTION_PARAMETERS_REQUIRED",
                    "error": "请提供交易所在的链和交易哈希。",
                }
            }
        registered_hash = state.get("broadcast_tx_hash")
        broadcast_status = None
        if registered_hash and str(registered_hash).lower() == str(tx_hash).lower():
            broadcast_status = state.get("broadcast_status")
        call = {
            "name": "transaction_status_tool",
            "args": {
                "chain": str(chain),
                "tx_hash": str(tx_hash),
                "broadcast_status": broadcast_status,
            },
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
            "response": await naturalize_read_response(
                state, {"kind": "transaction_status", **transaction}
            ),
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
        return {
            "gas_snapshot": gas,
            "response": await naturalize_read_response(state, {"kind": "gas_check", **gas}),
        }

    async def baseline_swap_gas_estimate(selected: Mapping[str, Any]) -> dict[str, Any] | None:
        """Estimate a read-only source-chain baseline without preparing a provider order."""
        source_asset = selected.get("source_asset")
        source_chain = source_asset.get("chain") if isinstance(source_asset, Mapping) else None
        if not source_chain:
            return None
        adapter = execution_adapter(str(source_chain))
        if adapter is None:
            adapter = execution_adapter(canonical_chain(str(source_chain)))
        if adapter is None or not hasattr(adapter, "estimate_fee"):
            return None
        try:
            fee = await adapter.estimate_fee(to=None, data=None)
        except Exception:
            return None
        estimate = _fee_dump(fee)
        estimate.update(
            {
                "estimate_type": "baseline",
                "exact": False,
                "note": "确认后准备交易时会刷新为精确 gas 预估。",
            }
        )
        return estimate

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
        task = hydrate_active_task(state)
        task_id = (
            str(task.get("task_id"))
            if task is not None
            else f"legacy:{state.get('conversation_id') or 'conversation'}:swap"
        )
        task_revision = int(task.get("revision") or 0) if task is not None else 0
        task_slippage = ((task or {}).get("slots") or {}).get("slippage_bps")
        summary = _swap_confirmation_summary(selected, slippage_bps=task_slippage)
        gas_estimate = await baseline_swap_gas_estimate(selected)
        confirmation = _confirmation_snapshot(
            action="swap",
            status="requested",
            summary=summary,
            ttl_seconds=runtime.confirmation_ttl_seconds,
            task_id=task_id,
            task_revision=task_revision,
        )
        return {
            "confirmation_state": confirmation,
            "task_stage": "awaiting_confirmation",
            "swap_gas_estimate": gas_estimate,
            "response": {
                "kind": "confirmation_required",
                "confirmation": confirmation,
                "quote": selected,
                "gas_estimate": gas_estimate,
            },
        }

    async def confirmation_wait(state: dict[str, Any]) -> dict[str, Any]:
        current = state.get("confirmation_state") or {}
        if _confirmation_stale(state, current):
            stale = {**current, "status": "stale", "reason": "task_revision_changed"}
            return {
                "confirmation_state": stale,
                "task_stage": "confirmation_stale",
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            "CONFIRMATION_STALE",
                            "任务参数已变化，请重新确认最新交易。",
                            retryable=True,
                        )
                    ],
                    "confirmation": stale,
                },
            }
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
                "gas_estimate": state.get("swap_gas_estimate"),
            }
        )
        if isinstance(answer, dict) and ("request" in answer or "message" in answer):
            incoming = answer.get("request")
            if not isinstance(incoming, Mapping):
                incoming = {"message": answer.get("message")}
            update: dict[str, Any] = {
                "request": _mapping(incoming),
                "forced_intent": None,
                "confirmation_state": None,
                "selected_quote": None,
                "quote_candidates": [{"__clear__": True}],
                "swap_request": None,
                "pending_transaction": None,
                "user_confirmation": None,
                "response_action": "reparse",
            }
            if isinstance(answer.get("wallet_context"), Mapping):
                update["wallet_context"] = _mapping(answer["wallet_context"])
            return update
        approved = isinstance(answer, dict) and bool(answer.get("approved"))
        if answer is True:
            approved = True
        updated = {
            **current,
            "status": "approved" if approved else "rejected",
            "reason": None if approved else "user_rejected",
        }
        if not approved:
            active_task = hydrate_active_task(state)
            if active_task:
                active_task = {
                    **active_task,
                    "status": "cancelled",
                    "stage": "cancelled",
                    "missing_fields": [],
                    "updated_by": "user",
                }
            cancelled = _task_state(
                state.get("conversation_state"),
                goal=str((active_task or {}).get("kind") or "swap"),
                stage="cancelled",
                slots=(active_task or {}).get("slots") or {},
                missing_fields=[],
                status="cancelled",
                updated_by="user",
            )
            return {
                "confirmation_state": updated,
                "user_confirmation": {"approved": False},
                "active_task": active_task,
                "conversation_state": cancelled,
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
            "response": await naturalize_read_response(
                state, {"kind": "asset_discovery", **snapshot}
            ),
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
            request_json = _dump(request)
            if not isinstance(request_json, dict):
                raise ValueError("swap request must be an object")
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
        quote_started = perf_counter()
        _emit_progress(
            "quote_provider",
            "started",
            message=f"正在向 {name} 请求兑换报价。",
            provider=str(name),
        )
        try:
            result = await asyncio.wait_for(
                quote_tool_node.ainvoke(
                    {"messages": [AIMessage(content="", tool_calls=[call])]}
                ),
                timeout=runtime.provider_timeout_seconds,
            )
        except TimeoutError:
            _emit_progress(
                "quote_provider",
                "failed",
                started=quote_started,
                message=f"{name} 报价请求超时。",
                provider=str(name),
                error_code="PROVIDER_QUOTE_TIMEOUT",
            )
            return {
                "quote_candidates": [],
                "errors": [
                    _error(
                        "PROVIDER_QUOTE_TIMEOUT",
                        f"{name} 报价超过 {runtime.provider_timeout_seconds:g} 秒，已跳过。",
                        retryable=True,
                        details={"provider": name},
                    )
                ],
            }
        try:
            payload = json.loads(result["messages"][-1].content)
        except (TypeError, ValueError):
            payload = {
                "ok": False,
                "code": "TOOL_OUTPUT_INVALID",
                "error": str(result["messages"][-1].content),
            }
        if not payload.get("ok"):
            provider_error = {
                "provider": str(name),
                "code": payload.get("code", "TOOL_FAILED"),
                "message": payload.get("error", "报价查询失败。"),
                "retryable": payload.get("retryable", False),
                "category": payload.get("category"),
            }
            _emit_progress(
                "quote_provider",
                "failed",
                started=quote_started,
                message=f"{name} 报价请求失败。",
                provider=str(name),
                error_code=payload.get("code", "TOOL_FAILED"),
            )
            return {
                "quote_candidates": [],
                "errors": [
                    _error(
                        "PROVIDER_QUOTE_FAILED",
                        f"{name} 报价失败：{payload.get('error', '报价查询失败。')}",
                        retryable=bool(payload.get("retryable")),
                        details=provider_error,
                    )
                ],
            }
        _emit_progress(
            "quote_provider",
            "completed",
            started=quote_started,
            message=f"{name} 报价请求完成。",
            provider=str(name),
        )
        return {"quote_candidates": [payload["quote"]]}

    async def intent(state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("forced_intent")
        request = _mapping(state.get("request"))
        message = str(request.get("message", ""))
        active_task = hydrate_active_task(state)
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
            "market_details",
            "price_history",
        }
        if _looks_like_cancel_message(message) and (
            active_task
            or state.get("conversation_state")
            or state.get("swap_draft")
            or state.get("transfer_draft")
            or state.get("swap_request")
            or state.get("selected_quote")
        ):
            kind = str((active_task or {}).get("kind") or "swap")
            if active_task:
                active_task = {
                    **active_task,
                    "status": "cancelled",
                    "stage": "cancelled",
                    "missing_fields": [],
                    "updated_by": "user",
                }
            cancelled = _task_state(
                state.get("conversation_state"),
                goal=kind,
                stage="cancelled",
                slots=(active_task or {}).get("slots") or {},
                missing_fields=[],
                status="cancelled",
                updated_by="user",
            )
            return {
                "intent": "clarification",
                "response_action": "cancelled",
                "forced_intent": None,
                "route": "response",
                "active_task": active_task,
                "conversation_state": cancelled,
                "task_stage": "cancelled",
                "swap_draft": {} if kind == "swap" else state.get("swap_draft"),
                "transfer_draft": {} if kind == "transfer" else state.get("transfer_draft"),
                "swap_request": None,
                "transfer_request": None,
                "token_candidates": [],
                "quote_candidates": [{"__clear__": True}],
                "selected_quote": None,
                "confirmation_state": None,
                "pending_transaction": None,
                "user_confirmation": None,
                "response": {"kind": "cancelled", "message": f"已取消当前{kind}任务。"},
            }
        if isinstance(state.get("preflight_followup"), Mapping):
            response = _preflight_balance_response(
                state["preflight_followup"],
                _response_language(message, state.get("conversation_history")),
            )
            return {
                "intent": "clarification",
                "route": "response",
                "response": response,
                "preflight_followup": None,
            }
        supervisor_source = (state.get("supervisor_output") or {}).get("source")
        if existing in valid and (
            not state.get("supervisor_output") or supervisor_source in {"api", "graph_resume"}
        ):
            return {"route": existing, "max_poll_attempts": runtime.max_poll_attempts}
        parsed = _parse_model_output(state.get("supervisor_output"))
        if parsed and parsed.get("source") in {"api", "graph_resume"}:
            # Forced/resumed requests may already contain structured fields;
            # reconstruct a minimal model output from the preserved request.
            parsed = {**parsed, "intent": existing or parsed.get("intent")}
        if not parsed:
            try:
                model_request = dict(request)
                model_request["wallet_context"] = state.get("wallet_context")
                model_request["active_task"] = active_task
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
        explicit_task_kind = {
            "transfer": "transfer",
            "swap_quote": "swap",
            # Rebuild a quote request after wallet connection completes the
            # missing sender/recipient context.
            "swap_prepare": "swap",
        }.get(intent_value)
        task_kind = explicit_task_kind
        if task_kind is None and intent_value == "clarification" and active_task:
            task_kind = active_task.get("kind")
        task_update: dict[str, Any] = {}
        if task_kind in {"transfer", "swap"}:
            lifecycle_active = bool(
                state.get("provider_orders")
                or state.get("broadcast_tx_hash")
                or state.get("selected_quote")
                or state.get("confirmation_state")
                or state.get("pending_transaction")
            )
            previous_kind = str((active_task or {}).get("kind") or "")
            previous_status = str((active_task or {}).get("status") or "")
            operation_reset = lifecycle_active and (
                previous_kind != task_kind
                or previous_status in {"completed", "cancelled"}
                or intent_value in {"transfer", "swap_quote"}
            )
            if operation_reset:
                prior_snapshot = _operation_snapshot(state)
                if prior_snapshot is not None:
                    prior_history = [
                        dict(item)
                        for item in (state.get("operation_history") or [])
                        if isinstance(item, Mapping)
                    ]
                    task_update["operation_history"] = [
                        *prior_history,
                        prior_snapshot,
                    ][-20:]
                # Preserve conversation history, but remove the previous
                # operation's irreversible lifecycle and authorization state.
                task_update.update(
                    operation_reset=True,
                    selected_quote=None,
                    quote_candidates=[{"__clear__": True}],
                    provider_orders={},
                    provider_order_ids={},
                    swap_draft=None,
                    transfer_draft=None,
                    status_snapshot=None,
                    transaction_query=None,
                    transaction_status_snapshot=None,
                    broadcast_tx_hash=None,
                    broadcast_status=None,
                    confirmation_state=None,
                    pending_transaction=None,
                    approval_transaction=None,
                    approval_tx_hash=None,
                    allowance_requirement=None,
                    preflight=None,
                    swap_gas_estimate=None,
                    errors=[{"__clear__": True}],
                )
            if (
                not active_task
                or active_task.get("kind") != task_kind
                or active_task.get("status") in {"completed", "cancelled"}
            ):
                active_task = new_active_task(task_kind)
            patch_source: Any = parsed
            if hasattr(runtime.model, "extract"):
                extraction_request = dict(request)
                extraction_request["wallet_context"] = state.get("wallet_context")
                extraction_request["active_task"] = active_task
                extraction_request["conversation_state"] = state.get("conversation_state")
                extraction_request["conversation_history"] = _model_conversation_history(
                    state.get("conversation_history")
                )
                metadata = request.get("metadata")
                suggestion_data = _suggestion_data_for_model(
                    metadata.get("suggestion_data") if isinstance(metadata, Mapping) else None
                )
                if suggestion_data is not None:
                    extraction_request["suggestion_data"] = suggestion_data
                extraction_started = perf_counter()
                _emit_progress(
                    "slot_extraction",
                    "started",
                    message="正在提取兑换参数。",
                    task_kind=task_kind,
                )
                try:
                    patch_source = runtime.model.extract(task_kind, extraction_request)
                    patch_source = (
                        await patch_source if hasattr(patch_source, "__await__") else patch_source
                    )
                except Exception as exc:
                    _emit_progress(
                        "slot_extraction",
                        "failed",
                        started=extraction_started,
                        message="请求参数提取失败。",
                        task_kind=task_kind,
                        error_code="SLOT_EXTRACTION_FAILED",
                    )
                    return {
                        "intent": "clarification",
                        "route": "clarification",
                        "active_task": active_task,
                        "errors": [_error("SLOT_EXTRACTION_FAILED", str(exc), retryable=True)],
                        "response": {
                            "kind": "clarification",
                            "message": "我没有可靠地识别出本轮参数，请换一种说法。",
                            "missing_fields": active_task.get("missing_fields") or [],
                        },
                        "max_poll_attempts": runtime.max_poll_attempts,
                    }
                _emit_progress(
                    "slot_extraction",
                    "completed",
                    started=extraction_started,
                    message="请求参数提取完成。",
                    task_kind=task_kind,
                )
            task_patch = _task_patch(task_kind, patch_source)
            prior_slots = (active_task or {}).get("slots", {})
            if task_kind == "transfer" and _self_recipient_hint(message):
                wallet_address = (state.get("wallet_context") or {}).get("address")
                if wallet_address:
                    task_patch["recipient"] = str(wallet_address)
            if task_kind == "swap":
                fiat_hint = mentioned_fiat_value(message)
                if (
                    fiat_hint is None
                    and not (active_task.get("slots") or {}).get("target_value_amount")
                    and any(token in message.lower() for token in ("计算", "换算", "calculate"))
                ):
                    fiat_hint = _recent_fiat_value(state.get("conversation_history"))
                if fiat_hint is not None and not {
                    "input_amount",
                    "output_amount",
                }.intersection(task_patch):
                    task_patch["target_value_amount"] = fiat_hint[0]
                    task_patch["target_value_currency"] = fiat_hint[1]
                explicit_slippage = _parse_slippage_bps(message)
                if explicit_slippage is not None:
                    task_patch["slippage_bps"] = explicit_slippage
                direction_hints = swap_direction_hints(message)
                for key, value in direction_hints.items():
                    if key in {"source_symbol", "destination_symbol"}:
                        opposite = (
                            "destination_symbol" if key == "source_symbol" else "source_symbol"
                        )
                        if canonical_symbol(str(task_patch.get(opposite) or "")) == value:
                            task_patch.pop(opposite, None)
                    task_patch[key] = value
                if (
                    "source_chain" in direction_hints
                    and "destination_symbol" in direction_hints
                    and "destination_chain" not in direction_hints
                ):
                    inferred_destination_chain = _unique_native_chain(
                        direction_hints["destination_symbol"],
                        task_patch.get("destination_token_address"),
                    )
                    if inferred_destination_chain is not None:
                        task_patch["destination_chain"] = inferred_destination_chain
                effective_slots = _merge_swap_slots(prior_slots, task_patch)
                reconciled_slots = _reconcile_unique_native_chains(effective_slots)
                for side in ("source", "destination"):
                    chain_key = f"{side}_chain"
                    if reconciled_slots.get(chain_key) != effective_slots.get(chain_key):
                        task_patch[chain_key] = reconciled_slots[chain_key]
            amount_key = "amount" if task_kind == "transfer" else "input_amount"
            explicit_amount = mentioned_amount_with_unit(message)
            parsed_amount = unambiguous_amount_with_unit(message)
            if task_kind == "swap" and explicit_amount is not None:
                explicit_value, explicit_unit = explicit_amount
                known_source = canonical_symbol(
                    str(task_patch.get("source_symbol") or prior_slots.get("source_symbol") or "")
                )
                known_destination = canonical_symbol(
                    str(
                        task_patch.get("destination_symbol")
                        or prior_slots.get("destination_symbol")
                        or ""
                    )
                )
                # A token-qualified amount selects its named side.  A
                # destination amount is an exact-output request and must be
                # preserved for provider reverse quoting.
                if explicit_unit == known_destination and explicit_unit != known_source:
                    task_patch.pop("input_amount", None)
                    task_patch.pop("input_amount_raw", None)
                    task_patch["output_amount"] = explicit_value
                    task_patch["amount_mode"] = "exact_out"
                elif explicit_unit == known_source and explicit_unit != known_destination:
                    task_patch[amount_key] = explicit_value
                    task_patch["amount_mode"] = "exact_in"
            elif task_kind == "transfer" and explicit_amount is not None:
                explicit_value, explicit_unit = explicit_amount
                known_symbol = canonical_symbol(
                    str(task_patch.get("symbol") or prior_slots.get("symbol") or "")
                )
                if not known_symbol or explicit_unit == known_symbol:
                    task_patch[amount_key] = explicit_value
            elif amount_key not in task_patch and parsed_amount is not None:
                fallback_amount, amount_unit = parsed_amount
                if task_kind == "transfer" or amount_unit is None:
                    task_patch[amount_key] = fallback_amount
                    if task_kind == "swap":
                        task_patch["amount_mode"] = "exact_in"
            if task_kind == "transfer" and amount_key not in task_patch:
                prior_symbol = canonical_symbol(str(prior_slots.get("symbol") or ""))
                next_symbol = canonical_symbol(str(task_patch.get("symbol") or prior_symbol))
                if prior_symbol and next_symbol != prior_symbol:
                    # A human amount belongs to the asset it qualified.  When
                    # the user changes assets with wording such as “一些 ETH”,
                    # reusing the old USDC amount is unsafe; require a fresh,
                    # explicit amount instead.
                    task_patch["amount"] = None
                    task_patch["amount_raw"] = None
            if task_kind == "transfer":
                prior_symbol = canonical_symbol(str(prior_slots.get("symbol") or ""))
                next_symbol = canonical_symbol(
                    str(task_patch.get("symbol") or prior_symbol or "")
                )
                if prior_symbol and next_symbol != prior_symbol and "chain" not in task_patch:
                    # A changed asset must not inherit the previous asset's
                    # network. Prefer the connected wallet network for its
                    # native asset; otherwise force the resolver to use the
                    # current wallet context instead of stale task state.
                    wallet_context = state.get("wallet_context") or {}
                    wallet_chain = wallet_context.get("chain")
                    if wallet_chain and _native_symbol(
                        str(wallet_chain), wallet_context
                    ) == next_symbol:
                        task_patch["chain"] = str(wallet_chain)
                    else:
                        task_patch["chain"] = None
            merged = merge_task_patch(active_task, task_patch)
            active_task = merged.task
            task_update = {
                **task_update,
                "active_task": active_task,
                **merged.invalidation,
            }
            if task_kind == "transfer" and any(
                state.get(field) is not None
                for field in (
                    "broadcast_tx_hash",
                    "broadcast_status",
                    "transaction_query",
                    "transaction_status_snapshot",
                )
            ):
                task_update.update(
                    broadcast_tx_hash=None,
                    broadcast_status=None,
                    transaction_query=None,
                    transaction_status_snapshot=None,
                )
            wallet_event(
                "slot_merge",
                "updated" if merged.changed_slots else "unchanged",
                duration_ms=0,
                state={
                    **state,
                    "active_task": active_task,
                    "predicted_intent": parsed["intent"],
                    "route": task_kind,
                },
            )
            intent_value = "transfer" if task_kind == "transfer" else "swap_quote"
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
            response_state = canonical_state(state)
            response_state["swap_draft"] = state.get("swap_draft") or {}
            response_state["transfer_draft"] = state.get("transfer_draft") or {}
            if active_task:
                if active_task.get("kind") == "swap":
                    response_state["swap_draft"] = project_legacy_draft(active_task)
                elif active_task.get("kind") == "transfer":
                    response_state["transfer_draft"] = project_legacy_draft(active_task)
            return {
                "intent": "clarification",
                "route": "clarification",
                "active_task": active_task,
                "response_action": "clarification",
                "max_poll_attempts": runtime.max_poll_attempts,
                **state_update,
                "response": await generate_clarification(
                    response_state,
                    intent="swap_quote" if looks_like_swap else "clarification",
                    missing=parsed.get("missing_fields") or [],
                    include_portfolio=looks_like_swap,
                ),
            }
        if intent_value == "swap_quote":
            draft = (
                project_legacy_draft(active_task)
                if active_task and active_task.get("kind") == "swap"
                else dict(state.get("swap_draft") or {})
            )
            if not draft and state.get("swap_request"):
                draft = _swap_draft_from_request(state.get("swap_request"))
            draft, token_candidates, resolution_errors = await _resolve_swap_assets(
                draft, runtime.providers
            )
            if active_task:
                active_task = _resolved_task(active_task, draft)
                task_update["active_task"] = active_task
            if resolution_errors:
                active_task = _task_progress(
                    active_task,
                    slots=draft,
                    status="collecting",
                    stage="resolving_assets",
                    missing_fields=[],
                )
                return {
                    "intent": "clarification",
                    "route": "response",
                    "swap_draft": draft,
                    **task_update,
                    "active_task": active_task,
                    "missing_fields": [],
                    **_task_update(
                        state.get("conversation_state"),
                        goal="swap",
                        stage="resolving_assets",
                        slots=draft,
                        missing_fields=[],
                    ),
                    "max_poll_attempts": runtime.max_poll_attempts,
                    "response": {
                        "kind": "error",
                        "message": "无法确认兑换资产，请检查 Token 和网络是否受支持。",
                        "errors": resolution_errors,
                    },
                }
            draft, value_errors = await _resolve_swap_target_value(
                draft, runtime.price_provider
            )
            if active_task:
                active_task = _resolved_task(active_task, draft)
                task_update["active_task"] = active_task
            if value_errors:
                active_task = _task_progress(
                    active_task,
                    slots=draft,
                    status="collecting",
                    stage="resolving_value",
                    missing_fields=[],
                )
                return {
                    "intent": "clarification",
                    "route": "response",
                    "swap_draft": draft,
                    **task_update,
                    "active_task": active_task,
                    "missing_fields": [],
                    **_task_update(
                        state.get("conversation_state"),
                        goal="swap",
                        stage="resolving_value",
                        slots=draft,
                        missing_fields=[],
                    ),
                    "max_poll_attempts": runtime.max_poll_attempts,
                    "response": {
                        "kind": "error",
                        "message": "无法按美元价值计算目标资产数量。",
                        "errors": value_errors,
                    },
                }
            if token_candidates:
                descriptions = [
                    (
                        f"{'来源' if item['side'] == 'source' else '目标'} "
                        f"{item['symbol']} ({item['chain']})"
                    )
                    for item in token_candidates
                ]
                return {
                    "intent": "clarification",
                    "route": "clarification",
                    "swap_draft": draft,
                    **task_update,
                    "active_task": _task_progress(
                        active_task,
                        slots=draft,
                        status="collecting",
                        stage="selecting_token",
                        missing_fields=[],
                    ),
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
                            "我找到了多个同名 Token。请确认你要使用哪个网络："
                            f"{'；'.join(descriptions)}。"
                        ),
                        "token_candidates": token_candidates,
                    },
                }
            request_model, missing = _swap_draft_request(draft, state.get("wallet_context"))
            if request_model is None:
                if missing == ["source_equals_destination"]:
                    return {
                        "intent": "clarification",
                        "route": "response",
                        "swap_draft": draft,
                        **task_update,
                        "active_task": _task_progress(
                            active_task,
                            slots=draft,
                            status="collecting",
                            stage="collecting_parameters",
                            missing_fields=[],
                        ),
                        "missing_fields": [],
                        "response": {
                            "kind": "error",
                            "errors": [
                                _error(
                                    "SOURCE_EQUALS_DESTINATION",
                                    "来源 Token 和目标 Token 不能是同一资产。",
                                )
                            ],
                        },
                    }
                user_missing = [
                    field
                    for field in missing
                    if field in _USER_SWAP_FIELDS and field not in _INTERNAL_ASSET_FIELDS
                ]
                if draft.get("target_value_amount"):
                    user_missing = [field for field in user_missing if field != "input_amount"]
                if "input_amount_raw" in missing and "input_amount" not in user_missing:
                    user_missing.append("input_amount")
                stage = "collecting_parameters" if user_missing else "resolving_assets"
                active_task = _task_progress(
                    active_task,
                    slots=draft,
                    status="collecting",
                    stage=stage,
                    missing_fields=user_missing,
                )
                common_update = {
                    "intent": "clarification",
                    "route": "clarification",
                    "swap_draft": draft,
                    **task_update,
                    "active_task": active_task,
                    "missing_fields": user_missing,
                    **_task_update(
                        state.get("conversation_state"),
                        goal="swap",
                        stage=stage,
                        slots=draft,
                        missing_fields=user_missing,
                    ),
                    "max_poll_attempts": runtime.max_poll_attempts,
                }
                if user_missing:
                    return {
                        **common_update,
                        "response": await generate_clarification(
                            {
                                **canonical_state(state),
                                "active_task": active_task,
                                "swap_draft": draft,
                            },
                            intent="swap_quote",
                            missing=user_missing,
                            include_portfolio=not bool(
                                draft.get("source_symbol") or draft.get("destination_symbol")
                            ),
                        ),
                    }
                unresolved_assets = [
                    field
                    for field in missing
                    if field in _INTERNAL_ASSET_FIELDS
                ]
                if unresolved_assets:
                    missing_networks = [
                        field
                        for field in ("source_chain", "destination_chain")
                        if draft.get(field) in (None, "")
                        and draft.get(field.replace("_chain", "_symbol"))
                    ]
                    if missing_networks:
                        message = _swap_clarification_message(
                            {"swap_draft": draft, "request": state.get("request") or {}},
                            missing_networks,
                            _response_language(message, state.get("conversation_history")),
                        )
                        return {
                            **common_update,
                            "intent": "clarification",
                            "route": "clarification",
                            "missing_fields": missing_networks,
                            "response": {
                                "kind": "clarification",
                                "message": message,
                                "missing_fields": missing_networks,
                            },
                        }
                errors = resolution_errors or [
                    _error(
                        "ASSET_RESOLUTION_FAILED",
                        "无法从已配置的兑换服务确认 Token 元数据。",
                    )
                ]
                return {
                    **common_update,
                    "response": {
                        "kind": "error",
                        "message": "无法确认兑换资产，请检查 Token 和网络是否受支持。",
                        "errors": errors,
                    },
                }
            active_task = _task_progress(
                active_task,
                slots=draft,
                status="ready",
                stage="ready_for_quote",
                missing_fields=[],
            )
            request_payload = request_model.model_dump(mode="json")
            if draft.get("output_amount"):
                request_payload.update(
                    {
                        "output_amount": str(draft["output_amount"]),
                        "amount_mode": "exact_out",
                    }
                )
            return {
                "intent": intent_value,
                "route": intent_value,
                "swap_draft": draft,
                **task_update,
                "active_task": active_task,
                "missing_fields": [],
                "swap_request": request_payload,
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
            active_task = _sanitize_native_transfer_task(active_task, state.get("wallet_context"))
            draft = (
                project_legacy_draft(active_task)
                if active_task and active_task.get("kind") == "transfer"
                else dict(state.get("transfer_draft") or {})
            )
            draft = await _resolve_transfer_asset(
                draft,
                runtime.providers,
                state.get("wallet_context"),
                runtime.wallet_provider,
            )
            request_model, missing = _transfer_draft_request(draft, state.get("wallet_context"))
            if request_model is None:
                canonical_slots = dict((active_task or {}).get("slots") or {})
                canonical_slots.update(
                    {
                        _TRANSFER_CANONICAL_KEYS.get(key, key): value
                        for key, value in draft.items()
                        if value is not None
                    }
                )
                active_task = _task_progress(
                    active_task,
                    slots=canonical_slots,
                    status="collecting",
                    stage="collecting_parameters",
                    missing_fields=missing,
                )
                return {
                    "intent": "clarification",
                    "route": "clarification",
                    "transfer_draft": draft,
                    **task_update,
                    "active_task": active_task,
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
                        **await generate_clarification(
                            {
                                **canonical_state(state),
                                "active_task": active_task,
                                "transfer_draft": draft,
                            },
                            intent="transfer",
                            missing=missing,
                            include_portfolio=False,
                        ),
                        "suggestions": await transfer_suggestions(
                            {**canonical_state(state), "transfer_draft": draft}, missing
                        ),
                    },
                }
            active_task = _task_progress(
                active_task,
                slots={
                    **dict((active_task or {}).get("slots") or {}),
                    **{
                        _TRANSFER_CANONICAL_KEYS.get(key, key): value
                        for key, value in draft.items()
                        if value is not None
                    },
                },
                status="ready",
                stage="ready_for_prepare",
                missing_fields=[],
            )
            return {
                "intent": intent_value,
                "route": intent_value,
                "transfer_draft": draft,
                **task_update,
                "active_task": active_task,
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
            previous_chain = draft.pop("transaction_chain", None)
            previous_hash = draft.pop("transaction_hash", None)
            if previous_chain is not None:
                draft["chain"] = previous_chain
            if previous_hash is not None:
                draft["tx_hash"] = previous_hash

            current_patch: dict[str, Any] = {}
            extraction_error: Exception | None = None
            patch_source: Any = parsed
            if hasattr(runtime.model, "extract"):
                extraction_request = dict(request)
                extraction_request["conversation_history"] = _model_conversation_history(
                    state.get("conversation_history")
                )
                extraction_started = perf_counter()
                _emit_progress(
                    "slot_extraction",
                    "started",
                    message="正在提取交易查询参数。",
                    task_kind="transaction_status",
                )
                try:
                    patch_source = runtime.model.extract("transaction_status", extraction_request)
                    patch_source = (
                        await patch_source if hasattr(patch_source, "__await__") else patch_source
                    )
                except Exception as exc:
                    extraction_error = exc
                    _emit_progress(
                        "slot_extraction",
                        "failed",
                        started=extraction_started,
                        message="交易查询参数提取失败。",
                        task_kind="transaction_status",
                        error_code="SLOT_EXTRACTION_FAILED",
                    )
                else:
                    _emit_progress(
                        "slot_extraction",
                        "completed",
                        started=extraction_started,
                        message="交易查询参数提取完成。",
                        task_kind="transaction_status",
                    )
            if extraction_error is None:
                extracted = _dump(patch_source)
                if isinstance(extracted, dict):
                    current_patch.update(
                        {
                            key: extracted[key]
                            for key in _TRANSACTION_QUERY_KEYS
                            if extracted.get(key) not in (None, "")
                        }
                    )
            current_patch.update(
                {
                    key: value
                    for key, value in transaction_query_hints(message).items()
                    if value not in (None, "")
                }
            )

            explicit_chain = current_patch.get("transaction_chain")
            explicit_hash = current_patch.get("transaction_hash")
            if explicit_chain is not None:
                draft["chain"] = explicit_chain
            if explicit_hash is not None:
                draft["tx_hash"] = explicit_hash

            registered_hash = state.get("broadcast_tx_hash")
            if registered_hash and explicit_hash is None:
                draft["tx_hash"] = str(registered_hash)
            if registered_hash and explicit_chain is None:
                pending = _mapping(state.get("pending_transaction"))
                selected = _mapping(state.get("selected_quote"))
                source_asset = _mapping(selected.get("source_asset"))
                inferred_chain = (
                    pending.get("chain")
                    or source_asset.get("chain")
                    or request.get("chain")
                    or (state.get("wallet_context") or {}).get("chain")
                )
                if inferred_chain:
                    draft["chain"] = str(inferred_chain)
            if draft.get("chain"):
                draft["chain"] = canonical_chain(str(draft["chain"]))
            pending = _mapping(state.get("pending_transaction"))
            if not registered_hash and explicit_hash is None and pending:
                if not draft.get("chain") and pending.get("chain"):
                    draft["chain"] = canonical_chain(str(pending["chain"]))
                return {
                    "intent": intent_value,
                    "route": intent_value,
                    "transaction_query": draft,
                    "max_poll_attempts": runtime.max_poll_attempts,
                }
            missing = [field for field in ("chain", "tx_hash") if not draft.get(field)]
            missing_fields = [
                "transaction_chain" if field == "chain" else "transaction_hash" for field in missing
            ]
            if missing:
                result: dict[str, Any] = {
                    "intent": "clarification",
                    "route": "clarification",
                    "transaction_query": draft,
                    "missing_fields": missing_fields,
                    "response": {
                        "kind": "clarification",
                        "message": "请提供交易所在链和交易哈希。",
                        "missing_fields": missing_fields,
                    },
                }
                if extraction_error is not None:
                    result["errors"] = [
                        _error(
                            "SLOT_EXTRACTION_FAILED",
                            str(extraction_error),
                            retryable=True,
                        )
                    ]
                return result
            return {
                "intent": intent_value,
                "route": intent_value,
                "transaction_query": draft,
                "max_poll_attempts": runtime.max_poll_attempts,
            }
        if intent_value == "price_query":
            raw_price = state.get("price_request") or {}
            current = dict(_mapping(raw_price))
            if hasattr(runtime.model, "extract"):
                extraction_request = dict(request)
                extraction_request["conversation_history"] = _model_conversation_history(
                    state.get("conversation_history")
                )
                try:
                    patch = runtime.model.extract("price", extraction_request)
                    patch = await patch if hasattr(patch, "__await__") else patch
                    extracted = _mapping(patch)
                    current.update(
                        {
                            key: value
                            for key, value in extracted.items()
                            if value not in (None, "")
                        }
                    )
                except Exception as exc:
                    return {
                        "response": {
                            "kind": "clarification",
                            "message": "请提供要查询价格的资产，例如 BTC 或 ETH。",
                            "errors": [
                                _error("PRICE_PARAMETERS_INVALID", str(exc), retryable=True)
                            ],
                        },
                        "route": "response",
                    }
            if not current.get("symbol"):
                return {
                    "response": {
                        "kind": "clarification",
                        "message": "请提供要查询价格的资产，例如 BTC 或 ETH。",
                        "missing_fields": ["symbol"],
                    },
                    "route": "response",
                }
            symbol = canonical_symbol(str(current["symbol"]))
            chain = current.get("chain") or (state.get("wallet_context") or {}).get("chain")
            return {
                "price_request": {
                    "chain": chain,
                    "symbol": symbol,
                    "decimals": current.get("decimals"),
                    "chain_id": current.get("chain_id"),
                    "address": current.get("token_address"),
                },
                "route": "price_query",
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
                "errors": [
                    _error(
                        "MISSING_SWAP_PARAMETERS",
                        "兑换参数尚未准备完整，请补充缺失信息。",
                    )
                ],
            }
        try:
            request = _request(raw)
        except Exception as exc:
            return {
                "intent": "clarification",
                "errors": [_error("INVALID_SWAP_PARAMETERS", str(exc))],
            }
        if _same_swap_asset(request):
            return {
                "swap_request": request.model_dump(mode="json"),
                "available_providers": [],
                "selected_quote": None,
                "quote_candidates": [{"__clear__": True}],
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            "SOURCE_EQUALS_DESTINATION",
                            "来源 Token 和目标 Token 不能是同一资产。",
                        )
                    ],
                },
            }
        request_payload = request.model_dump(mode="json")
        if isinstance(raw, Mapping) and raw.get("amount_mode") == "exact_out":
            request_payload.update(
                {
                    "amount_mode": "exact_out",
                    "output_amount": str(raw.get("output_amount")),
                }
            )
        selected = state.get("selected_quote")
        if state.get("intent") == "swap_quote" and not runtime.providers:
            return {
                "swap_request": request_payload,
                "available_providers": [],
                "selected_quote": selected,
                "response": None,
                "errors": [
                    _error(
                        "PROVIDER_UNAVAILABLE",
                        "没有可用的兑换 Provider。",
                        details={"capability": "quote"},
                    )
                ],
            }
        return {
            "swap_request": request_payload,
            "available_providers": list(runtime.providers),
            "selected_quote": selected,
            "response": None,
            "errors": [{"__clear__": True}],
        }

    async def quote_provider(state: dict[str, Any]) -> dict[str, Any]:
        return await quote_tool_call(state)

    async def quote_response(state: dict[str, Any]) -> dict[str, Any]:
        quotes = [_dump(item) for item in state.get("quote_candidates", [])]
        quotes = [item for item in quotes if isinstance(item, dict)]
        if not quotes and state.get("errors"):
            reverse_errors = [
                item for item in state["errors"] if item.get("code") == "REVERSE_QUOTE_UNAVAILABLE"
            ]
            if reverse_errors:
                return {
                    "selected_quote": None,
                    "response": {
                        "kind": "clarification",
                        "message": (
                            "当前兑换服务不能按目标到账数量反向询价，请直接提供要换出的数量。"
                        ),
                        "missing_fields": ["input_amount"],
                        "errors": state["errors"],
                    },
                }
            return {
                "selected_quote": None,
                "response": {"kind": "error", "errors": state["errors"]},
            }
        invalid_quotes: list[dict[str, Any]] = []
        valid_quotes: list[dict[str, Any]] = []
        for quote in quotes:
            try:
                expected_output = Decimal(str(quote.get("expected_output", "0")))
                minimum_output = quote.get("minimum_output")
                minimum_value = Decimal(str(minimum_output)) if minimum_output is not None else None
            except (ArithmeticError, TypeError, ValueError):
                expected_output = Decimal("0")
                minimum_value = Decimal("0")
            if expected_output <= 0 or (minimum_value is not None and minimum_value <= 0):
                invalid_quotes.append(
                    _error(
                        "INVALID_QUOTE",
                        "Provider 返回了无效报价：预期到账数量必须大于 0。",
                        retryable=True,
                        details={
                            "provider": quote.get("provider"),
                            "provider_reference": quote.get("provider_reference"),
                            "expected_output": quote.get("expected_output"),
                            "minimum_output": quote.get("minimum_output"),
                        },
                    )
                )
                continue
            valid_quotes.append(quote)
        quotes = valid_quotes
        if not quotes:
            return {
                "selected_quote": None,
                "quote_candidates": [{"__clear__": True}],
                "response": {
                    "kind": "error",
                    "errors": invalid_quotes
                    or [_error(
                        "NO_VALID_QUOTES",
                        "暂时没有可用的有效兑换报价。",
                        retryable=True,
                        details={"reason": "all_providers_returned_no_quote"},
                    )],
                },
            }
        previous_selected = _dump(state.get("selected_quote"))
        previous_reference = (
            previous_selected.get("provider_reference")
            if isinstance(previous_selected, dict)
            else None
        )
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
        # Select only after enrichment so confirmation/session projection keep
        # USD values and price snapshots. Multiple candidates still require an
        # explicit user choice.
        if len(quotes) == 1:
            selected = quotes[0]
        elif previous_reference:
            selected = next(
                (
                    item
                    for item in quotes
                    if item.get("provider_reference") == previous_reference
                ),
                previous_selected,
            )
        else:
            selected = None
        provider_errors = [
            item
            for item in state.get("errors", [])
            if item.get("code") in {"PROVIDER_QUOTE_FAILED", "PROVIDER_QUOTE_TIMEOUT"}
        ]
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
        quote_response = {
            "selected_quote": selected,
            "quote_candidates": [{"__clear__": True}, *quotes],
            **task_update,
            "response": {
                "kind": "swap_quote",
                "quotes": quotes,
                "price_snapshots": snapshot_map,
                "provider_errors": provider_errors,
            },
        }
        quote_response["response"] = await naturalize_read_response(
            state, quote_response["response"]
        )
        return quote_response

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
        adapter = execution_adapter(req.chain)
        if adapter is None and not (
            runtime.wallet_provider is not None
            and hasattr(runtime.wallet_provider, "get_token_balances")
        ):
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
                wallet_provider=runtime.wallet_provider,
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
            tx = _apply_fee_estimate(tx, preflight.get("fee_estimate"))
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
        def authorization_response(stage: str, **payload: Any) -> dict[str, Any]:
            response = {"stage": stage, **payload}
            if state.get("intent") == "swap_status":
                messages = {
                    "approval_pending": "Approve 交易已提交，正在等待链上确认。",
                    "approval_failed": "Approve 交易链上执行失败，请重新发起授权。",
                    "approval_required": "Approve 已确认，但授权额度仍不足，请重新授权。",
                    "swap_ready": "Approve 已确认，兑换交易已准备好，请在钱包中签名并广播。",
                }
                response.update(
                    kind="swap_status",
                    status=stage,
                    message=messages.get(stage, "兑换授权状态已更新。"),
                )
            return response

        def prepare_failure(provider_name: str, exc: Exception) -> dict[str, Any]:
            provider_code = str(getattr(exc, "code", "") or "")
            if provider_name == "omnibridge" and provider_code == "919":
                message = (
                    "OmniBridge 不接受当前报价的金额精度（最多 8 位小数）。"
                    "请重新获取报价后再选择 Provider。"
                )
            else:
                message = "兑换服务暂时无法准备交易，请重新获取报价后再试。"
            details = {"provider": provider_name}
            if provider_code:
                details["provider_code"] = provider_code
            error = _error(
                "PROVIDER_PREPARE_FAILED",
                message,
                retryable=True,
                details=details,
            )
            return {
                "authorization_stage": "failed",
                "response": {
                    "kind": "error",
                    "stage": "failed",
                    "message": message,
                    "errors": [error],
                },
            }

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
            try:
                prepared = await provider.prepare(quote)
            except Exception as exc:
                return prepare_failure(str(selected.get("provider")), exc)
            preflight = None
            request = state.get("swap_request") or {}
            source_asset = request.get("source_asset") or selected.get("source_asset") or {}
            source_chain = str(source_asset.get("chain") or getattr(prepared, "chain", ""))
            adapter = execution_adapter(source_chain)
            if isinstance(prepared, DepositOrder):
                if adapter is None:
                    raise ValueError(f"Chain adapter unavailable for {source_chain} deposit")
                prepared = _deposit_order_to_transaction(
                    prepared,
                    adapter=adapter,
                    sender=str(request.get("sender_address") or ""),
                )
            if isinstance(prepared, UnsignedTransaction) and adapter is not None:
                preflight = await _transaction_preflight(
                    adapter=adapter,
                    chain=source_chain,
                    sender=str(request.get("sender_address") or ""),
                    recipient=(
                        prepared.display.get("deposit_address")
                        or str(request.get("recipient_address") or "")
                    ),
                    amount_raw=str(request.get("input_amount_raw") or "0"),
                    token=(
                        Asset.model_validate(source_asset) if source_asset.get("address") else None
                    ),
                    transaction=prepared,
                    wallet_context=state.get("wallet_context"),
                    wallet_provider=runtime.wallet_provider,
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
                        },
                    }
            prepared = _apply_fee_estimate(
                prepared, preflight.get("fee_estimate") if preflight else None
            )
            dumped = _dump(prepared)
            return {
                "pending_transaction": dumped,
                "preflight": preflight,
                "authorization_stage": "swap_ready",
                "response": authorization_response(
                    "swap_ready",
                    pending_transaction=dumped,
                    **({"preflight": preflight} if preflight is not None else {}),
                ),
            }
        adapter = execution_adapter(requirement.token.chain)
        if adapter is None and not (
            runtime.wallet_provider is not None
            and hasattr(runtime.wallet_provider, "get_token_balances")
        ):
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
                    "response": authorization_response(
                        "approval_required",
                        approval_transaction=approval_model.model_dump(mode="json"),
                    ),
                }
        if approval_tx_hash:
            receipt = await adapter.get_transaction_receipt(str(approval_tx_hash))
            success = receipt_success(receipt)
            if success is False or receipt is None:
                return {
                    "approval_transaction": approval_transaction,
                    "approval_tx_hash": approval_tx_hash,
                    "allowance_requirement": requirement.model_dump(mode="json"),
                    "authorization_stage": "approval_pending"
                    if receipt is None
                    else "approval_failed",
                    "response": authorization_response(
                        "approval_pending" if receipt is None else "approval_failed",
                        approval_transaction=approval_transaction,
                    ),
                }
            allowance_raw = await adapter.get_allowance(
                requirement.token, requirement.owner, requirement.spender
            )
            if int(allowance_raw) < required:
                return {
                    "approval_transaction": approval_transaction,
                    "approval_tx_hash": approval_tx_hash,
                    "allowance_requirement": requirement.model_dump(mode="json"),
                    "authorization_stage": "approval_required",
                    "response": authorization_response(
                        "approval_required",
                        approval_transaction=approval_transaction,
                    ),
                }
        provider = runtime.providers.get(str(selected.get("provider")))
        if provider is None:
            return {
                "response": {
                    "stage": "failed",
                    "errors": [_error("PROVIDER_UNAVAILABLE", "Provider unavailable")],
                }
            }
        try:
            prepared = await provider.prepare(quote)
        except Exception as exc:
            return prepare_failure(str(selected.get("provider")), exc)
        preflight = None
        request = state.get("swap_request") or {}
        source_asset = request.get("source_asset") or selected.get("source_asset") or {}
        source_chain = str(source_asset.get("chain") or getattr(prepared, "chain", ""))
        adapter = execution_adapter(source_chain)
        if isinstance(prepared, DepositOrder):
            if adapter is None:
                raise ValueError(f"Chain adapter unavailable for {source_chain} deposit")
            prepared = _deposit_order_to_transaction(
                prepared,
                adapter=adapter,
                sender=str(request.get("sender_address") or requirement.owner),
            )
        if isinstance(prepared, UnsignedTransaction) and adapter is not None:
            preflight = await _transaction_preflight(
                adapter=adapter,
                chain=source_chain,
                sender=str(request.get("sender_address") or requirement.owner),
                recipient=(
                    prepared.display.get("deposit_address")
                    or str(request.get("recipient_address") or requirement.owner)
                ),
                amount_raw=str(request.get("input_amount_raw") or "0"),
                token=(Asset.model_validate(source_asset) if source_asset.get("address") else None),
                transaction=prepared,
                wallet_context=state.get("wallet_context"),
                wallet_provider=runtime.wallet_provider,
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
                    },
                }
            prepared = _apply_fee_estimate(
                prepared, preflight.get("fee_estimate") if preflight else None
            )
            dumped = _dump(prepared)
            return {
                "approval_transaction": approval_transaction,
                "approval_tx_hash": approval_tx_hash,
                "allowance_requirement": requirement.model_dump(mode="json"),
                "pending_transaction": dumped,
                "preflight": preflight,
                "authorization_stage": "swap_ready",
                "response": authorization_response(
                    "swap_ready",
                    pending_transaction=dumped,
                    **({"preflight": preflight} if preflight is not None else {}),
                ),
            }

    async def wallet_query(state: dict[str, Any]) -> dict[str, Any]:
        request = state.get("request", {})
        chain = str(request.get("chain", ""))
        address = str(request.get("address", ""))
        if runtime.wallet_provider is not None and hasattr(
            runtime.wallet_provider, "get_token_balances"
        ):
            index = okx_chain_index(chain)
            if index is None:
                return {
                    "errors": [_error("OKX_CHAIN_UNSUPPORTED", f"Chain {chain} is unavailable.")]
                }
            try:
                balances = await runtime.wallet_provider.get_token_balances(address, [index])
                native = next((item for item in balances if item.asset.address is None), None)
                snapshot = {
                    "address": address,
                    "chain": chain,
                    "native_balance": _dump(native) if native else None,
                    "token_balances": [
                        _dump(item) for item in balances if item.asset.address is not None
                    ],
                    "source": "okx",
                }
                return {
                    "wallet_context": snapshot,
                    "response": {"kind": "wallet_query", "wallet": snapshot},
                }
            except Exception as exc:
                return {"errors": [_error("WALLET_QUERY_FAILED", str(exc))]}
        return {
            "errors": [
                _error("OKX_CAPABILITY_UNAVAILABLE", "OKX wallet data provider unavailable.")
            ]
        }

    async def portfolio_query(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("portfolio_request") or {}
        context = state.get("wallet_context") or {}
        chain = str(raw.get("chain") or context.get("chain") or "").upper()
        address = str(raw.get("address") or context.get("address") or "")
        adapter = None
        if runtime.wallet_provider is None or not hasattr(
            runtime.wallet_provider, "get_token_balances"
        ):
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            "OKX_CAPABILITY_UNAVAILABLE",
                            "OKX wallet data provider unavailable.",
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
        if runtime.wallet_provider is not None and hasattr(
            runtime.wallet_provider, "get_token_balances"
        ):
            index = okx_chain_index(chain)
            if index is None:
                return {
                    "response": {
                        "kind": "error",
                        "errors": [
                            _error("OKX_CHAIN_UNSUPPORTED", f"Chain {chain} is unavailable.")
                        ],
                    }
                }
            try:
                balances = await runtime.wallet_provider.get_token_balances(address, [index])
            except Exception as exc:
                return {
                    "response": {
                        "kind": "error",
                        "errors": [_error("PORTFOLIO_QUERY_FAILED", str(exc), retryable=True)],
                    }
                }
        else:
            balances = []
        try:
            if adapter is not None:
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
                "source": "okx" if runtime.wallet_provider is not None else "chain",
            }
            if price_error:
                snapshot["price_error"] = "价格服务暂时不可用。"
            return {
                "portfolio_snapshot": snapshot,
                "response": await naturalize_read_response(
                    state, {"kind": "portfolio_query", "portfolio": snapshot}
                ),
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
        adapter = execution_adapter(chain)
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
                "response": await naturalize_read_response(
                    state, {"kind": "gas_check", **snapshot}
                ),
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
            "response": await naturalize_read_response(
                state,
                {
                    "kind": "asset_discovery",
                    "assets": result_assets,
                    "query": snapshot["query"],
                    "provider_errors": provider_errors,
                },
            ),
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
            asset, error = _normalize_price_asset(raw, {}, state.get("wallet_context"))
            if error and error.get("code") == "PRICE_ASSET_UNRESOLVED":
                discovered, candidates = await _resolve_asset_metadata(
                    raw, runtime.providers, state.get("wallet_context")
                )
                if discovered is not None:
                    asset, error = discovered, None
                elif candidates:
                    error = {
                        "code": "PRICE_ASSET_AMBIGUOUS",
                        "error": "该网络存在多个同名资产。请补充资产名称以便区分。",
                        "symbol": raw.get("symbol"),
                        "chain": raw.get("chain"),
                        "candidates": candidates,
                    }
            if error:
                return {
                    "response": {
                        "kind": "clarification",
                        "message": error["error"],
                        "missing_fields": [],
                        "errors": [_error(error["code"], error["error"])],
                    }
                }
            prices = await runtime.price_provider.get_prices([asset])
            return {
                "response": await naturalize_read_response(
                    state,
                    {"kind": "price_query", "prices": [_dump(item) for item in prices]},
                )
            }
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
        explorer = runtime.explorer_provider
        if explorer is not None and hasattr(explorer, "get_transaction_detail"):
            try:
                detail = await explorer.get_transaction_detail(chain, tx_hash)
                status_value = getattr(detail.status, "value", str(detail.status))
                snapshot = {
                    "chain": chain,
                    "tx_hash": tx_hash,
                    "status": status_value,
                    "message": {
                        "pending": "交易已提交，正在等待链上确认。",
                        "confirmed": "交易已确认。",
                        "failed": "交易执行失败或已回滚。",
                        "unknown": "暂时无法确定交易状态。",
                    }.get(status_value, "暂时无法确定交易状态。"),
                    "source": "okx",
                    "detail": _dump(detail),
                }
                return {
                    "transaction_status_snapshot": snapshot,
                    "response": await naturalize_read_response(
                        state, {"kind": "transaction_status", **snapshot}
                    ),
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
        return {
            "response": {
                "kind": "error",
                "errors": [
                    _error(
                        "OKX_CAPABILITY_UNAVAILABLE",
                        "OKX transaction explorer unavailable.",
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
        confirmation_state = state.get("confirmation_state") or {}
        if confirmation_state and _confirmation_stale(state, confirmation_state):
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error(
                            "CONFIRMATION_STALE",
                            "任务参数已变化，请重新确认最新交易。",
                            retryable=True,
                        )
                    ],
                }
            }
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
            request = state.get("swap_request") or {}
            source_asset = request.get("source_asset") or selected.get("source_asset") or {}
            source_chain = str(source_asset.get("chain") or selected.get("chain") or "")
            adapter = execution_adapter(source_chain)
            if isinstance(prepared, DepositOrder):
                if adapter is None:
                    raise ValueError(f"Chain adapter unavailable for {source_chain} deposit")
                prepared = _deposit_order_to_transaction(
                    prepared,
                    adapter=adapter,
                    sender=str(request.get("sender_address") or ""),
                )
            if isinstance(prepared, dict) and {"to", "data"}.issubset(prepared):
                prepared_transaction = UnsignedTransaction.model_validate(prepared)
            elif hasattr(prepared, "to") and hasattr(prepared, "data"):
                prepared_transaction = prepared
            else:
                prepared_transaction = None
            if prepared_transaction is not None and adapter is not None:
                preflight = await _transaction_preflight(
                    adapter=adapter,
                    chain=source_chain,
                    sender=str(request.get("sender_address") or ""),
                    recipient=(
                        prepared_transaction.display.get("deposit_address")
                        or str(request.get("recipient_address") or "")
                    ),
                    amount_raw=str(request.get("input_amount_raw") or "0"),
                    token=(
                        Asset.model_validate(source_asset) if source_asset.get("address") else None
                    ),
                    transaction=prepared_transaction,
                    wallet_context=state.get("wallet_context"),
                    wallet_provider=runtime.wallet_provider,
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
            prepared = _apply_fee_estimate(
                prepared, preflight.get("fee_estimate") if preflight else None
            )
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
        broadcast_status = state.get("broadcast_status")
        provider_visible = {"broadcast_seen", "broadcast_pending", "confirmed"}
        if broadcast_status and broadcast_status not in provider_visible:
            response_payload = {
                "kind": "swap_status",
                "status": broadcast_status,
                "broadcast_status": broadcast_status,
                "tx_hash": tx_hash,
                "message": (
                    "Source-chain transaction propagation is not confirmed yet; "
                    "provider order polling has not started."
                ),
            }
            return {
                "response": await naturalize_read_response(state, response_payload)
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
            pending = state.get("pending_transaction")
            provider_reference = (
                pending.get("provider_reference")
                if isinstance(pending, Mapping)
                else getattr(pending, "provider_reference", None)
            ) or selected.get("provider_reference", "")
            order = await provider.register_broadcast(str(provider_reference), tx_hash)
            value = _dump(order)
            pending_payload = (
                _mapping(pending).get("provider_payload") if pending is not None else {}
            )
            if isinstance(pending_payload, Mapping) and pending_payload:
                order_payload = _mapping(value.get("provider_payload"))
                value["provider_payload"] = {
                    **pending_payload,
                    **{
                        str(key): item
                        for key, item in order_payload.items()
                        if item not in (None, "")
                    },
                }
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
        broadcast_status = state.get("broadcast_status")
        provider_visible = {"broadcast_seen", "broadcast_pending", "confirmed", None}
        if state.get("broadcast_tx_hash") and broadcast_status not in provider_visible:
            response_payload = {
                "kind": "swap_status",
                "status": broadcast_status,
                "broadcast_status": broadcast_status,
                "tx_hash": state.get("broadcast_tx_hash"),
                "message": (
                    "Source-chain transaction propagation is not confirmed yet; "
                    "provider order polling has not started."
                ),
            }
            return {
                "response": await naturalize_read_response(state, response_payload),
                "poll_attempts": state.get("poll_attempts", 0),
            }
        orders = state.get("provider_orders", {})
        if not orders:
            if state.get("pending_transaction"):
                response_payload = {
                    "kind": "swap_status",
                    "status": "swap_ready",
                    "message": "兑换交易已准备好，正在等待钱包签名并广播。",
                    "pending_transaction": state["pending_transaction"],
                }
                return {
                    "response": await naturalize_read_response(state, response_payload)
                }
            response_payload = {
                "kind": "swap_status",
                "status": "unknown",
                "message": status_messages["unknown"],
            }
            return {
                "response": await naturalize_read_response(state, response_payload),
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
            provider_status = dumped.get("status", "unknown")
            broadcast_message = {
                "broadcast_seen": "源链已收到交易广播",
                "broadcast_pending": "源链交易已广播，正在等待链上确认",
                "confirmed": "源链交易已确认",
            }.get(broadcast_status)
            provider_message = status_messages.get(
                provider_status, "兑换订单状态已更新，建议稍后重新查询。"
            )
            if provider_status == "processing":
                provider_message = "兑换服务仍在处理中，目标链资产尚未到账。"
            elif provider_status == "pending":
                provider_message = "兑换订单已提交，目标链资产尚未到账，Provider 仍在处理中。"
            message = (
                f"{broadcast_message}；{provider_message}"
                if broadcast_message
                else provider_message
            )
            response_payload = {
                "kind": "swap_status",
                "status": dumped,
                "provider_status": provider_status,
                "broadcast_status": broadcast_status,
                "confirmation_status": broadcast_status,
                "tx_hash": state.get("broadcast_tx_hash"),
                "message": message,
            }
            return {
                "status_snapshot": dumped,
                "poll_attempts": attempts,
                "response": await naturalize_read_response(state, response_payload),
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
        intent = str(state.get("intent") or "")
        if state.get("errors"):
            return {"response": {"kind": "error", "errors": state["errors"]}}
        if intent in {
            "wallet_query",
            "portfolio_query",
            "price_query",
            "gas_check",
            "asset_discovery",
            "transaction_status",
            "swap_status",
            "swap_quote",
            "market_details",
            "price_history",
        }:
            return {
                "response": await naturalize_read_response(
                    state,
                    {"kind": intent, "errors": state.get("errors", [])},
                )
            }
        if state.get("intent") == "clarification":
            response: dict[str, Any] = {
                "kind": "clarification",
                "errors": state.get("errors", []),
            }
            if not response["errors"] and not state.get("missing_fields"):
                response["message"] = _response_fallback(
                    "clarification",
                    [],
                    _response_language(
                        str(
                            (state.get("request") or {}).get("message")
                            or state.get("message")
                            or ""
                        )
                    ),
                )
            return {"response": response}
        if state.get("intent") == "unsupported":
            message = str(
                (state.get("request") or {}).get("message") or state.get("message") or ""
            )
            return {
                "response": {
                    "kind": "unsupported",
                    "message": _response_fallback(
                        "unsupported", [], _response_language(message)
                    ),
                }
            }
        return {"response": {"kind": state.get("intent", "clarification")}}

    async def _react_read_observation(
        name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        """Reuse the validated read implementation behind a ReAct tool call."""
        read_state = {
            **state,
            "read_native": False,
            "read_tool_call": {"name": name, "arguments": arguments},
            "read_facts": {},
            "read_tool_calls": [],
            "read_loop_count": 0,
        }
        update = await read_only_tool(read_state)
        facts = update.get("read_facts") or {}
        value = facts.get(name)
        if isinstance(value, Mapping):
            return dict(value)
        return {
            "ok": False,
            "code": "READ_TOOL_FAILED",
            "error": "只读工具没有返回 JSON 对象。",
        }

    async def react_wallet_balance(
        chain: str | None = None,
        address: str | None = None,
        symbol: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        return await _react_read_observation(
            "wallet_balance",
            {"chain": chain, "address": address, "symbol": symbol},
            state,
        )

    async def react_portfolio_snapshot(
        address: str | None = None,
        chain: str | None = None,
        symbol: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        return await _react_read_observation(
            "portfolio_snapshot",
            {"address": address, "chain": chain, "symbol": symbol},
            state,
        )

    async def react_price_lookup(
        symbol: str,
        chain: str | None = None,
        token_address: str | None = None,
        decimals: int | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        return await _react_read_observation(
            "price_lookup",
            {
                "symbol": symbol,
                "chain": chain,
                "token_address": token_address,
                "decimals": decimals,
            },
            state,
        )

    async def react_market_details(
        symbol: str,
        chain: str | None = None,
        token_address: str | None = None,
        decimals: int | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        return await _react_read_observation(
            "market_details",
            {
                "symbol": symbol,
                "chain": chain,
                "token_address": token_address,
                "decimals": decimals,
            },
            state,
        )

    async def react_price_history(
        symbol: str,
        chain: str | None = None,
        token_address: str | None = None,
        decimals: int | None = None,
        period: str | None = None,
        limit: int | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        return await _react_read_observation(
            "price_history",
            {
                "symbol": symbol,
                "chain": chain,
                "token_address": token_address,
                "decimals": decimals,
                "period": period,
                "limit": limit,
            },
            state,
        )

    async def react_asset_discovery(
        chain: str | None = None,
        search: str | None = None,
        provider_name: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        return await _react_read_observation(
            "asset_discovery",
            {"chain": chain, "search": search, "provider_name": provider_name},
            state,
        )

    async def react_gas_estimate(
        chain: str | None = None,
        address: str | None = None,
        to: str | None = None,
        data: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        return await _react_read_observation(
            "gas_estimate",
            {"chain": chain, "address": address, "to": to, "data": data},
            state,
        )

    async def react_transaction_status(
        tx_hash: str,
        chain: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        return await _react_read_observation(
            "transaction_status", {"tx_hash": tx_hash, "chain": chain}, state
        )

    async def react_get_swap_quote(
        source_symbol: str | None = None,
        destination_symbol: str | None = None,
        input_amount: str | None = None,
        output_amount: str | None = None,
        amount_mode: str | None = None,
        source_chain: str | None = None,
        destination_chain: str | None = None,
        source_token_address: str | None = None,
        destination_token_address: str | None = None,
        slippage_bps: int | None = None,
        provider_name: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        """Resolve human asset names and request one or all provider quotes."""
        if amount_mode not in (None, "exact_in", "exact_out"):
            return {
                "ok": False,
                "code": "INVALID_AMOUNT_MODE",
                "error": "amount_mode 只能是 exact_in 或 exact_out。",
            }
        # A natural-language request such as “BNB 换 10u 的 ETH” does not
        # identify the destination network. Never silently turn it into a
        # same-chain token swap; that can produce provider errors or quote a
        # different asset than the user intended.
        current = state
        context = current.get("wallet_context") or {}
        task_slots = (current.get("active_task") or {}).get("slots") or {}
        values = {
            **dict(task_slots),
            "source_symbol": source_symbol,
            "destination_symbol": destination_symbol,
            "input_amount": input_amount,
            "output_amount": output_amount,
            "amount_mode": amount_mode,
            "source_chain": source_chain,
            "destination_chain": destination_chain,
            "source_token_address": source_token_address,
            "destination_token_address": destination_token_address,
            "slippage_bps": slippage_bps,
        }
        # Preserve the user's fiat-target semantics when the model calls the
        # tool directly. The lifecycle path performs this conversion later,
        # but ReAct must not reinterpret “10u” as an input token amount.
        if output_amount in (None, "") and input_amount in (None, ""):
            message = str((current.get("request") or {}).get("message") or "")
            fiat_hint = mentioned_fiat_value(message)
            if fiat_hint is not None:
                values["target_value_amount"] = fiat_hint[0]
                values["target_value_currency"] = fiat_hint[1]
                values["amount_mode"] = "exact_out"
        draft = {key: value for key, value in values.items() if value not in (None, "")}
        if draft.get("target_value_amount") and not draft.get("destination_chain"):
            return {
                "ok": False,
                "code": "DESTINATION_CHAIN_REQUIRED",
                "missing_fields": ["destination_chain"],
                "message": "请先确认目标资产所在的网络，才能按 10 USD 计算到账数量。",
            }
        draft.setdefault("source_chain", context.get("chain"))
        if not draft.get("destination_chain"):
            return {
                "ok": False,
                "code": "DESTINATION_CHAIN_REQUIRED",
                "missing_fields": ["destination_chain"],
                "message": "请先确认目标资产所在的网络（例如以太坊主网或 BSC）。",
            }
        draft.setdefault("sender_address", context.get("address"))
        draft.setdefault("recipient_address", context.get("address"))
        resolved, candidates, errors = await _resolve_swap_assets(draft, runtime.providers)
        if candidates:
            return {"ok": False, "code": "ASSET_AMBIGUOUS", "candidates": candidates}
        if errors:
            return {"ok": False, "code": "ASSET_RESOLUTION_FAILED", "errors": errors}
        request_model, missing = _swap_draft_request(resolved, context)
        if request_model is None:
            return {
                "ok": False,
                "code": "SWAP_PARAMETERS_REQUIRED",
                "missing_fields": missing,
                "message": "请补充兑换所需的资产、网络和数量。",
            }
        raw_request = request_model.model_dump(mode="json")
        if amount_mode == "exact_out" and output_amount is not None:
            raw_request.update(
                {"amount_mode": "exact_out", "output_amount": str(output_amount)}
            )
        providers = [str(provider_name)] if provider_name else list(runtime.providers)
        if not providers:
            return {
                "ok": False,
                "code": "PROVIDER_UNAVAILABLE",
                "error": "没有可用的兑换 Provider。",
            }
        quotes: list[dict[str, Any]] = []
        provider_errors: list[dict[str, Any]] = []
        for name in providers:
            result = await quote_tool_call(
                {"provider_name": name, "swap_request": raw_request}
            )
            quotes.extend(
                item
                for item in (result.get("quote_candidates") or [])
                if isinstance(item, Mapping) and _quote_is_usable(item)
            )
            provider_errors.extend(result.get("errors") or [])
        return {
            "ok": bool(quotes),
            "quotes": quotes,
            "provider_errors": provider_errors,
            "request": _dump(raw_request),
            "code": None if quotes else "NO_VALID_QUOTES",
        }

    async def react_prepare_transfer(
        chain: str | None = None,
        symbol: str | None = None,
        amount: str | None = None,
        recipient: str | None = None,
        token_address: str | None = None,
        decimals: int | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        current = state
        context = current.get("wallet_context") or {}
        draft = {
            "chain": chain or context.get("chain"),
            "symbol": symbol,
            "amount": amount,
            "recipient": recipient,
            "token_address": token_address,
            "decimals": decimals,
            "sender": context.get("address"),
        }
        resolved = await _resolve_transfer_asset(
            draft, runtime.providers, context, runtime.wallet_provider
        )
        request_model, missing = _transfer_draft_request(resolved, context)
        if request_model is None:
            return {
                "ok": False,
                "code": "TRANSFER_PARAMETERS_REQUIRED",
                "missing_fields": missing,
                "message": "请补充转账网络、资产、数量和收款地址。",
            }
        result = await transfer(
            {
                **current,
                "request": {"message": ""},
                "transfer_request": request_model.model_dump(mode="json"),
            }
        )
        return {"ok": result.get("response", {}).get("kind") == "transfer_prepare", **result}

    async def react_query_swap_status(
        tx_hash: str,
        chain: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        return await transaction_status_tool(
            str(chain or state.get("wallet_context", {}).get("chain") or ""),
            tx_hash,
            state.get("broadcast_status"),
        )

    react_tools = [
        StructuredTool.from_function(
            coroutine=react_wallet_balance,
            name="wallet_balance",
            description="查询钱包余额；用户指定资产时只返回该资产。网络和地址可由上下文提供。",
        ),
        StructuredTool.from_function(
            coroutine=react_portfolio_snapshot,
            name="portfolio_snapshot",
            description="查询一个网络或全部支持网络中美元价值非零的钱包资产。",
        ),
        StructuredTool.from_function(
            coroutine=react_price_lookup,
            name="price_lookup",
            description="查询资产当前美元价格，优先使用网络和 symbol，服务端解析合约与精度。",
        ),
        StructuredTool.from_function(
            coroutine=react_market_details,
            name="market_details",
            description="查询资产行情详情。",
        ),
        StructuredTool.from_function(
            coroutine=react_price_history,
            name="price_history",
            description="查询资产历史价格。",
        ),
        StructuredTool.from_function(
            coroutine=react_asset_discovery,
            name="asset_discovery",
            description="按网络和 symbol 搜索资产元数据。",
        ),
        StructuredTool.from_function(
            coroutine=react_gas_estimate,
            name="gas_estimate",
            description="估算交易网络手续费。",
        ),
        StructuredTool.from_function(
            coroutine=react_transaction_status,
            name="transaction_status",
            description="查询链上交易状态。",
        ),
        StructuredTool.from_function(
            coroutine=react_get_swap_quote,
            name="get_swap_quote",
            description="根据来源/目标资产和数量获取兑换报价；自动发现合约和 decimals。",
        ),
        StructuredTool.from_function(
            coroutine=react_prepare_transfer,
            name="prepare_transfer",
            description="准备未签名转账交易；自动发现 Token 合约和 decimals。",
        ),
        StructuredTool.from_function(
            coroutine=react_query_swap_status,
            name="query_swap_status",
            description="查询兑换或交易的当前状态。",
        ),
    ]

    react_tool_runner = ToolNode(
        react_tools,
        name="react_tools",
        handle_tool_errors=False,
    )

    async def run_react_tools(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state.get("messages") or [])
        latest = messages[-1] if messages else None
        calls = list(getattr(latest, "tool_calls", None) or [])
        started = perf_counter()
        status = "success"
        error_code = None
        try:
            result = await asyncio.wait_for(
                react_tool_runner.ainvoke(state),
                timeout=runtime.react_tool_timeout_seconds,
            )
            tool_messages = list(result.get("messages") or [])
            if not tool_messages:
                raise ValueError("tool node returned no ToolMessage")
        except TimeoutError:
            status = "timeout"
            error_code = "REACT_TOOL_TIMEOUT"
            tool_messages = [
                ToolMessage(
                    content=json.dumps(
                        {
                            "ok": False,
                            "code": error_code,
                            "error": "工具查询超过时限，已停止本轮调用。",
                        },
                        ensure_ascii=False,
                    ),
                    name=str(call.get("name") or "unknown_tool"),
                    tool_call_id=str(call.get("id") or ""),
                )
                for call in calls
            ]
        except Exception:
            status = "error"
            error_code = "REACT_TOOL_FAILED"
            # Avoid returning exception strings that may contain provider
            # URLs, request payloads, or credentials to the model/transcript.
            tool_messages = [
                ToolMessage(
                    content=json.dumps(
                        {
                            "ok": False,
                            "code": error_code,
                            "error": "工具执行失败，请基于此结果向用户说明暂时无法完成。",
                        },
                        ensure_ascii=False,
                    ),
                    name=str(call.get("name") or "unknown_tool"),
                    tool_call_id=str(call.get("id") or ""),
                )
                for call in calls
            ]

        def safe_tool_message(message: ToolMessage) -> ToolMessage:
            """Keep tool observations JSON-safe without leaking provider errors."""
            try:
                payload = json.loads(str(message.content))
            except (TypeError, ValueError):
                return ToolMessage(
                    content=json.dumps(
                        {
                            "ok": False,
                            "code": "TOOL_OUTPUT_INVALID",
                            "error": "工具返回了无法解析的结果。",
                        },
                        ensure_ascii=False,
                    ),
                    name=str(getattr(message, "name", "") or "unknown_tool"),
                    tool_call_id=str(getattr(message, "tool_call_id", "") or ""),
                )
            if not isinstance(payload, Mapping):
                payload = {
                    "ok": False,
                    "code": "TOOL_OUTPUT_INVALID",
                    "error": "工具返回了非 JSON 对象。",
                }
            else:
                payload = dict(payload)
                for error_key in ("errors", "provider_errors"):
                    entries = payload.get(error_key)
                    if not isinstance(entries, list):
                        continue
                    safe_entries = []
                    for entry in entries:
                        if isinstance(entry, Mapping):
                            safe_entry = dict(entry)
                            if "error" in safe_entry:
                                safe_entry["error"] = "下游 Provider 返回了错误。"
                            if "message" in safe_entry:
                                safe_entry["message"] = "下游 Provider 返回了错误。"
                            safe_entries.append(safe_entry)
                        else:
                            safe_entries.append({"code": "TOOL_PROVIDER_ERROR"})
                    payload[error_key] = safe_entries
                if payload.get("ok") is False:
                    payload["error"] = "工具查询失败，请根据错误码向用户说明当前无法完成。"
            return ToolMessage(
                content=json.dumps(payload, ensure_ascii=False, default=str),
                name=str(getattr(message, "name", "") or "unknown_tool"),
                tool_call_id=str(getattr(message, "tool_call_id", "") or ""),
            )

        tool_messages = [safe_tool_message(item) for item in tool_messages]

        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        timings = []
        for call in calls:
            call_id = str(call.get("id") or "")
            tool_name = str(call.get("name") or "unknown_tool")
            observation = next(
                (
                    item
                    for item in tool_messages
                    if str(getattr(item, "tool_call_id", "")) == call_id
                ),
                None,
            )
            observed_code = error_code
            observed_status = status
            cache_status = None
            if status == "success" and observation is not None:
                try:
                    payload = json.loads(str(observation.content))
                except (TypeError, ValueError):
                    payload = {}
                if isinstance(payload, Mapping) and payload.get("ok") is False:
                    observed_status = "error"
                    observed_code = str(payload.get("code") or "TOOL_FAILED")
                if isinstance(payload, Mapping):
                    cache_status = str(payload.get("cache_status") or "") or None
            timings.append(
                {
                    "call_id": call_id,
                    "tool": tool_name,
                    "elapsed_ms": elapsed_ms,
                    "status": observed_status,
                    "error_code": observed_code,
                    "cache_status": cache_status,
                }
            )
            if runtime.metrics is not None:
                runtime.metrics.record_tool(
                    tool_name,
                    elapsed_ms=elapsed_ms,
                    status=observed_status,
                    error_code=observed_code,
                    cache_status=cache_status,
                )
        return {
            "messages": tool_messages,
            "react_tool_timings": [*list(state.get("react_tool_timings") or []), *timings],
            "graph_role": "conversation",
            "route_mode": "react",
            "route_reason": "tool_result",
        }

    def route_entry(state: dict[str, Any]) -> str:
        request = state.get("request") or {}
        # Forced inputs are the explicit HTTP lifecycle boundary. All ordinary
        # messages go to the model. A production graph must fail loudly when
        # the configured model cannot provide the ReAct contract.
        if state.get("forced_intent") or not request.get("message"):
            return "legacy"
        if callable(getattr(runtime.model, "invoke_react", None)):
            return "react"
        if runtime.require_react:
            raise RuntimeError(
                "configured model does not support the required ReAct contract"
            )
        return "legacy"

    async def react_entry(state: dict[str, Any]) -> dict[str, Any]:
        request = dict(state.get("request") or {})
        current = str(request.get("message") or state.get("message") or "")
        existing = list(state.get("messages") or [])
        system = SystemMessage(
            content=(
                "你是多链数字货币钱包助手。根据完整对话和 ToolMessage 自主完成任务，"
                "需要事实时调用工具，可连续调用多个工具；不要索要工具可以发现的"
                "合约地址或 decimals。询问资产持仓价值时必须查询余额和价格并计算；"
                "指定资产只回答该资产，询问全部余额时返回所有美元价值非零资产。"
                "短追问要结合历史理解；工具失败时说明真实原因并提出最小澄清；不要编造事实或描述内部推理。"
            )
        )
        if not existing or not any(isinstance(item, SystemMessage) for item in existing):
            history_messages: list[Any] = [system]
            for item in _model_conversation_history(state.get("conversation_history")):
                role = str(item.get("role") or "")
                content = str(item.get("content") or "")
                if role == "user":
                    history_messages.append(HumanMessage(content=content))
                elif role == "assistant":
                    history_messages.append(AIMessage(content=content))
            if not any(
                isinstance(item, HumanMessage) and str(item.content) == current
                for item in history_messages
            ):
                history_messages.append(HumanMessage(content=current))
            return {
                "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *history_messages],
                "react_steps": 0,
                "react_tool_signatures": [],
                "react_tool_timings": [],
                "react_active": True,
                "graph_role": "conversation",
                "route_mode": "react",
                "route_reason": "ordinary_natural_language",
                # Keep the explicit quote-selection lifecycle contract
                # present on ordinary ReAct turns as well.  ReAct can expose
                # quote_candidates for the UI, but it must not implicitly
                # select a quote or enter the confirmation flow.
                "selected_quote": None,
            }
        last = existing[-1] if existing else None
        additions: list[Any] = []
        if not isinstance(last, HumanMessage) or str(last.content) != current:
            additions.append(HumanMessage(content=current))
        return {
            "messages": additions,
            "react_steps": 0,
            "react_tool_signatures": [],
            "react_tool_timings": [],
            "react_active": True,
            "graph_role": "conversation",
            "route_mode": "react",
            "route_reason": "ordinary_natural_language",
            # A conversational turn may offer fresh quote candidates, but
            # selection remains an explicit client/API action.
            "selected_quote": None,
        }

    async def react_agent(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state.get("messages") or [])
        step = int(state.get("react_steps") or 0) + 1
        if step > runtime.max_react_steps:
            previous = list(state.get("react_messages") or state.get("messages") or [])
            valid_quotes = _valid_react_quotes(previous)
            if valid_quotes:
                return {
                    "messages": [
                        AIMessage(
                            content=(
                                "我已找到可用的兑换报价，请查看下方报价并选择一个继续。"
                            )
                        )
                    ],
                    "react_steps": step,
                    "react_active": False,
                    "react_step_limit_with_result": True,
                    "graph_role": "conversation",
                    "route_mode": "react",
                    "route_reason": "react_step_limit_with_result",
                }
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "我已完成有限次数的查询，但仍无法可靠完成这次请求，"
                            "请补充具体资产或网络。"
                        )
                    )
                ],
                "react_steps": step,
                "react_active": False,
                "graph_role": "conversation",
                "route_mode": "react",
                "route_reason": "react_step_limit",
            }
        request_model_id = str((state.get("request") or {}).get("model_id") or "") or None
        invoke_react = runtime.model.invoke_react
        parameters = inspect.signature(invoke_react).parameters.values()
        accepts_model_id = any(
            parameter.name == "model_id"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if accepts_model_id:
            result = await invoke_react(messages, react_tools, model_id=request_model_id)
        else:
            result = await invoke_react(messages, react_tools)
        calls = list(getattr(result, "tool_calls", None) or [])
        prior = list(state.get("react_tool_signatures") or [])
        signatures = [
            read_tool_signature(str(call.get("name") or ""), call.get("args") or {})
            for call in calls
        ]
        if calls and signatures and all(signature in prior for signature in signatures):
            result = AIMessage(
                content=(
                    "我已经查询过相同的数据，但没有获得新的信息。"
                    "请补充或更换网络/资产后再试。"
                )
            )
            calls = []
        return {
            "messages": [result],
            "react_steps": step,
            "react_tool_signatures": [*prior, *[item for item in signatures if item not in prior]],
            "react_active": bool(calls),
            "graph_role": "conversation",
            "route_mode": "react",
            "route_reason": "tool_call" if calls else "model_answer",
        }

    async def finalize_response(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state.get("messages") or [])
        last = messages[-1] if messages else None
        content = str(getattr(last, "content", "") or "").strip()
        if not content:
            content = "我暂时无法生成有效回复，请换一种方式描述你的需求。"
        tool_payloads: list[tuple[str, dict[str, Any]]] = []
        current_user_index = max(
            (
                index
                for index, message in enumerate(messages)
                if isinstance(message, HumanMessage)
            ),
            default=-1,
        )
        turn_messages = messages[current_user_index + 1 :] if current_user_index >= 0 else messages
        for message in turn_messages:
            if not isinstance(message, ToolMessage):
                continue
            try:
                payload = json.loads(str(message.content))
            except (TypeError, ValueError):
                continue
            if isinstance(payload, Mapping):
                tool_payloads.append((str(message.name or ""), dict(payload)))
        react_results = [
            {"tool": name, "result": payload} for name, payload in tool_payloads
        ]
        response: dict[str, Any] = {
            "kind": "react",
            "message": content,
            "react_results": react_results,
            "react_tool_timings": list(state.get("react_tool_timings") or []),
            "graph_role": "conversation",
            "route_mode": state.get("route_mode") or "react",
            "route_reason": state.get("route_reason") or "model_answer",
        }
        update: dict[str, Any] = {"response": response, "react_active": False}
        quote_result = _best_react_quote_result(tool_payloads)
        if quote_result is not None:
            # These are tool observations, not a code-inferred intent. Project
            # them only so the existing explicit quote-selection endpoint can
            # continue the user-approved lifecycle.
            update["quote_candidates"] = [
                {"__clear__": True},
                *[dict(item) for item in quote_result["quotes"] if isinstance(item, Mapping)],
            ]
            if isinstance(quote_result.get("request"), Mapping):
                update["swap_request"] = dict(quote_result["request"])
        transfer_result = next(
            (
                payload
                for name, payload in reversed(tool_payloads)
                if name == "prepare_transfer"
                and payload.get("ok")
                and isinstance(payload.get("pending_transaction"), Mapping)
            ),
            None,
        )
        if transfer_result is not None:
            update["pending_transaction"] = dict(transfer_result["pending_transaction"])
            if isinstance(transfer_result.get("preflight"), Mapping):
                update["preflight"] = dict(transfer_result["preflight"])
        return update

    return locals()
