"""Small graph nodes and runtime adapters."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
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
from wallet_agent.domain.addresses import EVM_ALIASES, is_valid_address
from wallet_agent.domain.chains import (
    asset_logo_fields,
    chain_logo_url,
    native_decimals,
    native_symbol,
    token_logo_url,
)
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
from wallet_agent.portfolio import query_portfolio

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
        # The App returns the chosen BTC 出金地址 here when the user taps one
        # of the addresses in the select_btc_address surface.
        "sender_address",
        # The App may pin a swap channel (provider) here, e.g. "bridgers",
        # so the backend quotes and prepares through only that provider.
        "provider",
    },
    "transfer": {"intent", "chain", "symbol", "token_address", "decimals", "amount", "recipient"},
}


def _looks_like_asset_symbol(value: Any) -> bool:
    """A plausible ticker: an uppercase letter followed by letters/digits.

    Guards against a stray numeric token (e.g. a "0" the user mistyped as an
    amount) being carried into a suggestion's ``symbol`` slot — the amount card
    would otherwise render the coin as "0". Rejects pure digits, empty values
    and over-long noise; accepts ETH/USDT/TRX-style symbols.
    """
    text = str(value or "").strip().upper()
    if not text or len(text) > 12:
        return False
    return re.fullmatch(r"[A-Z][A-Z0-9]*", text) is not None


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
    # Compare chains through their canonical name so a suggestion that uses a
    # provider alias (ARB) still matches the App's declared ARBITRUM.
    chains = {canonical_chain(str(value)) for value in facts.get("supported_chains", [])}
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
        if clean.get("symbol") is not None and not _looks_like_asset_symbol(
            clean["symbol"]
        ):
            # A "symbol" that is pure digits (a mistyped amount) must never
            # reach the coin/amount card. Drop the suggestion instead of
            # rendering a "0" coin (App-side report).
            continue
        invalid = False
        for key in ("source_chain", "destination_chain", "chain"):
            if (
                clean.get(key) is not None
                and chains
                and canonical_chain(str(clean[key])) not in chains
            ):
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


async def _deposit_order_to_transaction(
    order: Any,
    *,
    adapter: Any,
    sender: str,
    from_addresses: Sequence[str] | None = None,
    wallet_context: Mapping[str, Any] | None = None,
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
        transaction = await _resolve(
            builder(
                token=order.source_asset,
                from_address=sender,
                to_address=order.deposit_address,
                amount_raw=order.input_amount_raw,
            )
        )
    else:
        builder = getattr(adapter, "build_native_transfer", None)
        if builder is None:
            raise ValueError(
                f"{order.source_asset.chain} adapter cannot build a native deposit transaction"
            )
        native_kwargs: dict[str, Any] = {}
        if from_addresses:
            native_kwargs["from_addresses"] = list(from_addresses)
        signing_info = (
            _utxo_signing_info(wallet_context, order.source_asset.chain)
            if _is_utxo_adapter(adapter)
            else {}
        )
        if signing_info:
            native_kwargs["signing_info"] = signing_info
        transaction = await _resolve(
            builder(
                from_address=sender,
                to_address=order.deposit_address,
                amount_raw=order.input_amount_raw,
                **native_kwargs,
            )
        )
    # Merge (not replace) so adapter-specific fields such as the Bitcoin PSBT
    # or fee breakdown survive alongside the provider deposit metadata.
    display = {
        **dict(getattr(transaction, "display", {}) or {}),
        "action": "provider_deposit",
        "provider": order.provider,
        "deposit_address": order.deposit_address,
        "input_amount": str(order.input_amount),
        "input_amount_raw": order.input_amount_raw,
        "provider_order_id": order.provider_order_id,
    }
    if getattr(order, "memo", None):
        display["memo"] = str(order.memo)
    return transaction.model_copy(
        update={
            "provider": order.provider,
            "provider_reference": order.provider_reference,
            "expires_at": order.expires_at,
            "display": display,
        }
    )


def _is_utxo_adapter(adapter: Any) -> bool:
    """Bitcoin-style chains sign locally built UTXO transactions, not calldata."""
    return getattr(adapter, "transaction_model", None) == "utxo"


def _is_account_adapter(adapter: Any) -> bool:
    """Account-model chains (TRON, Solana) sign node-built protobuf transactions."""
    return getattr(adapter, "transaction_model", None) == "account"


def _utxo_deposit_order(quote: Any, prepared: Any) -> DepositOrder | None:
    """Reinterpret an EVM-shaped provider payload as a UTXO deposit transfer.

    Bitcoin swap providers return only the deposit address (in ``txData.to``);
    there is no calldata to sign. The wallet must instead pay that address with a
    locally built native (PSBT) transfer, which mirrors a deposit order.
    """
    if isinstance(prepared, DepositOrder):
        return prepared
    if not isinstance(prepared, UnsignedTransaction):
        return None
    display = dict(getattr(prepared, "display", {}) or {})
    if display.get("action") in {"native_transfer", "provider_deposit"}:
        # Already a source-chain-ready transaction built by the chain adapter.
        return None
    deposit_address = str(
        display.get("deposit_address")
        or display.get("depositAddress")
        or getattr(prepared, "to", "")
        or ""
    ).strip()
    if not deposit_address:
        return None
    return DepositOrder(
        provider=getattr(prepared, "provider", None) or quote.provider,
        provider_order_id=quote.provider_reference,
        deposit_address=deposit_address,
        source_asset=quote.source_asset,
        destination_asset=quote.destination_asset,
        input_amount=quote.input_amount,
        input_amount_raw=quote.input_amount_raw,
        expires_at=getattr(prepared, "expires_at", None) or quote.expires_at,
        provider_reference=quote.provider_reference,
        # The provider's memo must survive the rebuild, or the App loses the
        # routing hint the provider (Bridgers, UTXO chains) sends with the
        # deposit address.
        memo=str(display.get("memo")) if display.get("memo") else None,
    )


async def _account_contract_swap(
    prepared: Any, *, adapter: Any, sender: str
) -> Any:
    """Rebuild a provider's EVM-shaped calldata as an account-model transaction.

    Bridgers returns TRON swap payloads in EVM shape (``{to, data, value}``), but
    account-model wallets (TronLink) sign the node-built protobuf
    (``raw_data_hex``). Rebuild the calldata through the adapter's contract-call
    builder so the App receives a signable transaction; already chain-ready
    payloads (a deposit order handled above, or a transaction the adapter built
    itself) pass through unchanged.
    """
    if not isinstance(prepared, UnsignedTransaction):
        return prepared
    display = dict(getattr(prepared, "display", {}) or {})
    if display.get("action") in {"contract_call", "native_transfer", "provider_deposit"}:
        return prepared
    calldata = str(getattr(prepared, "data", "") or "")
    if not calldata:
        return prepared
    builder = getattr(adapter, "build_smart_contract_call", None)
    if builder is None or not sender:
        return prepared
    transaction = await _resolve(
        builder(
            from_address=sender,
            contract_address=prepared.to,
            calldata=calldata,
            value_raw=str(getattr(prepared, "value", "0") or "0"),
        )
    )
    if not isinstance(transaction, UnsignedTransaction):
        return prepared
    merged = {
        **dict(getattr(transaction, "display", {}) or {}),
        **display,
        "provider": prepared.provider or display.get("provider") or "",
        "provider_order_id": prepared.provider_reference or "",
    }
    return transaction.model_copy(
        update={
            "provider": prepared.provider,
            "provider_reference": prepared.provider_reference,
            "expires_at": prepared.expires_at,
            "display": merged,
        }
    )


async def _resolve_swap_transaction(
    prepared: Any,
    *,
    quote: Any,
    adapter: Any,
    sender: str,
    chain: str,
    wallet_context: Mapping[str, Any] | None = None,
) -> Any:
    """Normalize a provider's prepared output into a source-chain transaction.

    EVM chains sign the provider's calldata directly. UTXO chains (Bitcoin)
    cannot, so a provider deposit payload is rebuilt as a native transfer paying
    the deposit address. Account-model chains (TRON, Solana) cannot sign raw EVM
    calldata either, so a provider's EVM-shaped swap payload is rebuilt through
    the adapter's contract-call builder into the node-built protobuf.
    """
    if _is_utxo_adapter(adapter):
        sources = _utxo_source_addresses(wallet_context, chain, sender)
        if sources and not is_valid_address(str(chain or ""), sender):
            # A UTXO swap is signed by the App's own account for the chain, not
            # by the browser-connected account of another chain family.
            sender = sources[0]
        order = _utxo_deposit_order(quote, prepared)
        if order is None:
            return prepared
        return await _deposit_order_to_transaction(
            order,
            adapter=adapter,
            sender=sender,
            from_addresses=sources,
            wallet_context=wallet_context,
        )
    if isinstance(prepared, DepositOrder):
        if adapter is None:
            raise ValueError(f"Chain adapter unavailable for {chain} deposit")
        return await _deposit_order_to_transaction(prepared, adapter=adapter, sender=sender)
    if _is_account_adapter(adapter):
        return await _account_contract_swap(prepared, adapter=adapter, sender=sender)
    return prepared


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


async def _resolve(value: Any) -> Any:
    """Chain adapters may build transactions synchronously or over HTTP."""
    return await value if inspect.isawaitable(value) else value


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


def _default_quote(
    quotes: list[Mapping[str, Any]], request: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Deterministically default one quote so the App skips the channel picker.

    Once coin/amount/chain are complete, the design renders the confirmation
    card straight away with a default channel instead of asking the user to
    compare providers. For an exact-in swap pick the highest expected output;
    for an exact-out swap pick the lowest input amount. Ties break on the
    provider reference so the default is stable across retries.
    """
    usable = [dict(item) for item in quotes if _quote_is_usable(item)]
    if not usable:
        return None
    exact_out = False
    if isinstance(request, Mapping):
        exact_out = (
            str(request.get("amount_mode") or "") == "exact_out"
            or (
                request.get("output_amount") not in (None, "")
                and not request.get("input_amount")
            )
        )

    def amount(quote: Mapping[str, Any], field: str) -> Decimal:
        try:
            return Decimal(str(quote.get(field) or "0"))
        except (ArithmeticError, TypeError, ValueError):
            return Decimal(0)

    field = "input_amount" if exact_out else "expected_output"

    def sort_key(quote: Mapping[str, Any]) -> tuple[Decimal, str]:
        return amount(quote, field), str(quote.get("provider_reference") or "")

    return (min if exact_out else max)(usable, key=sort_key)


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


_SWAP_STABLECOIN_SYMBOLS = ("USDT", "USDC")
_SWAP_PAIR_LIMIT = 3
_SWAP_DEFAULT_PAIRS = (
    ({"chain": "ETH", "symbol": "ETH"}, {"chain": "ETH", "symbol": "USDT"}),
    ({"chain": "ETH", "symbol": "USDC"}, {"chain": "ETH", "symbol": "ETH"}),
)
_COMMON_ASSET_SYMBOLS = {
    "BTC", "ETH", "BNB", "TRX", "SOL", "USDT", "USDC", "DAI", "LTC", "DOGE",
    "XRP", "ADA", "AVAX", "MATIC", "POL", "ARB", "OP", "TON", "BUSD", "FDUSD",
    "WBTC", "WETH", "SHIB", "PEPE", "DOT", "LINK", "UNI", "AAVE", "SUI", "APT",
}


def _swap_row_usd(row: Mapping[str, Any]) -> Decimal:
    try:
        return Decimal(str(row.get("usd_value") or "0"))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal(0)


def _swap_row_positive(row: Mapping[str, Any]) -> bool:
    """A row counts as a holding when its amount (or USD value) is non-zero."""
    amount = row.get("amount")
    if amount is not None:
        try:
            if Decimal(str(amount)) > 0:
                return True
        except (ArithmeticError, TypeError, ValueError):
            pass
    return _swap_row_usd(row) > 0


def _swap_asset_descriptor(
    chain: str | None, symbol: str | None, row: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A chip's source/destination descriptor: chain+symbol plus known metadata.

    Only real balance rows carry address/decimals/logo; a synthesized side (the
    stablecoin leg of a recommended pair) keeps chain+symbol and lets
    ``get_swap_quote`` resolve the contract, so we never invent an address.
    """
    canonical_chain_name = canonical_chain(str(chain or ""))
    canonical_symbol_name = canonical_symbol(str(symbol or ""))
    asset = row.get("asset") if isinstance(row, Mapping) else None
    if isinstance(asset, Mapping):
        resolved_chain = canonical_chain_name or canonical_chain(str(asset.get("chain") or ""))
        resolved_symbol = canonical_symbol_name or canonical_symbol(
            str(asset.get("symbol") or "")
        )
        descriptor = {"chain": resolved_chain, "symbol": resolved_symbol}
        if asset.get("address"):
            descriptor["address"] = str(asset["address"])
        if asset.get("decimals") is not None:
            descriptor["decimals"] = int(asset["decimals"])
        descriptor.update(
            asset_logo_fields(resolved_chain, resolved_symbol, asset.get("logo_url"))
        )
        return descriptor
    descriptor = {"chain": canonical_chain_name, "symbol": canonical_symbol_name}
    descriptor.update(asset_logo_fields(canonical_chain_name, canonical_symbol_name))
    return descriptor


def _swap_balance_rows(
    wallet_context: Mapping[str, Any],
    tool_payloads: list[tuple[str, dict[str, Any]]] | None,
    read_facts: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Non-zero balance rows usable for swap-pair recommendations.

    Sources in order of freshness: this turn's balance observations, the cached
    read-only snapshot, then the App-provided wallet balances.
    """
    rows: list[dict[str, Any]] = []
    for name, payload in tool_payloads or []:
        if name not in ("portfolio_snapshot", "wallet_balance") or not payload.get("ok"):
            continue
        portfolio = payload.get("portfolio")
        if isinstance(portfolio, Mapping):
            rows.extend(portfolio.get("assets") or [])
    if not rows and isinstance(read_facts, Mapping):
        snapshot = read_facts.get("portfolio_snapshot")
        if isinstance(snapshot, Mapping) and snapshot.get("ok"):
            portfolio = snapshot.get("portfolio")
            if isinstance(portfolio, Mapping):
                rows.extend(portfolio.get("assets") or [])
    if not rows:
        token_balances = wallet_context.get("token_balances")
        if isinstance(token_balances, list):
            rows.extend(token_balances)
        native_balance = wallet_context.get("native_balance")
        if isinstance(native_balance, Mapping):
            rows.append(native_balance)
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        asset = item.get("asset")
        if not isinstance(asset, Mapping):
            continue
        chain = canonical_chain(str(asset.get("chain") or ""))
        symbol = canonical_symbol(str(asset.get("symbol") or ""))
        address = str(asset.get("address") or "").lower()
        if not chain or not symbol:
            continue
        key = (chain, symbol, address)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _swap_recommended_pairs(
    rows: list[Mapping[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Recommend swap pairs from the user's non-zero balances.

    Sells each top non-stable holding for a stablecoin on the same chain and,
    when the user holds a stablecoin, offers buying the top holding back with
    it. Returns ``(source, destination)`` descriptors.
    """
    holdings = [
        item
        for item in rows
        if canonical_symbol(str((item.get("asset") or {}).get("symbol") or ""))
        not in _SWAP_STABLECOIN_SYMBOLS
        and _swap_row_positive(item)
    ]
    stables = [
        item
        for item in rows
        if canonical_symbol(str((item.get("asset") or {}).get("symbol") or ""))
        in _SWAP_STABLECOIN_SYMBOLS
        and _swap_row_positive(item)
    ]
    holdings.sort(key=_swap_row_usd, reverse=True)
    stables.sort(key=_swap_row_usd, reverse=True)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(source: dict[str, Any], destination: dict[str, Any]) -> None:
        key = (
            source.get("chain") or "",
            source.get("symbol") or "",
            destination.get("chain") or "",
            destination.get("symbol") or "",
        )
        if key in seen or len(pairs) >= _SWAP_PAIR_LIMIT:
            return
        seen.add(key)
        pairs.append((source, destination))

    for holding in holdings:
        if len(pairs) >= _SWAP_PAIR_LIMIT:
            break
        asset = holding.get("asset") or {}
        chain = canonical_chain(str(asset.get("chain") or ""))
        symbol = canonical_symbol(str(asset.get("symbol") or ""))
        stable_row = next(
            (
                item
                for item in stables
                if canonical_chain(str((item.get("asset") or {}).get("chain") or "")) == chain
            ),
            None,
        )
        stable_symbol = (
            canonical_symbol(str((stable_row.get("asset") or {}).get("symbol") or ""))
            if stable_row
            else "USDT"
        )
        add(
            _swap_asset_descriptor(chain, symbol, holding),
            (
                _swap_asset_descriptor(chain, stable_symbol, stable_row)
                if stable_row
                else _swap_asset_descriptor(chain, stable_symbol, None)
            ),
        )
    for stable in stables:
        if len(pairs) >= _SWAP_PAIR_LIMIT:
            break
        asset = stable.get("asset") or {}
        chain = canonical_chain(str(asset.get("chain") or ""))
        stable_symbol = canonical_symbol(str(asset.get("symbol") or ""))
        holding = next(
            (
                item
                for item in holdings
                if canonical_chain(str((item.get("asset") or {}).get("chain") or "")) == chain
            ),
            None,
        )
        if holding is None:
            holding = holdings[0] if holdings else None
        if holding is None:
            break
        holding_asset = holding.get("asset") or {}
        add(
            _swap_asset_descriptor(chain, stable_symbol, stable),
            _swap_asset_descriptor(
                canonical_chain(str(holding_asset.get("chain") or "")),
                canonical_symbol(str(holding_asset.get("symbol") or "")),
                holding,
            ),
        )
    return pairs


def _swap_pair_label(source: Mapping[str, Any], destination: Mapping[str, Any]) -> str:
    source_chain = str(source.get("chain") or "")
    source_symbol = str(source.get("symbol") or "")
    destination_chain = str(destination.get("chain") or "")
    destination_symbol = str(destination.get("symbol") or "")
    if source_chain and source_chain == destination_chain:
        return f"{source_symbol} → {destination_symbol}"
    return f"{source_chain} {source_symbol} → {destination_chain} {destination_symbol}"


def _swap_pair_message(
    source: Mapping[str, Any], destination: Mapping[str, Any], language: str | None
) -> str:
    source_chain = str(source.get("chain") or "")
    source_symbol = str(source.get("symbol") or "")
    destination_chain = str(destination.get("chain") or "")
    destination_symbol = str(destination.get("symbol") or "")
    if source_chain and source_chain == destination_chain:
        from_text, to_text = source_symbol, destination_symbol
    else:
        from_text = f"{source_chain} {source_symbol}"
        to_text = f"{destination_chain} {destination_symbol}"
    if language == "en":
        return f"Swap {from_text} to {to_text}"
    return f"把 {from_text} 换成 {to_text}"


def _swap_pair_chip(
    source: Mapping[str, Any], destination: Mapping[str, Any], language: str | None
) -> dict[str, Any]:
    return {
        "type": "swap_pair",
        "label": _swap_pair_label(source, destination),
        "message": _swap_pair_message(source, destination, language),
        "value": (
            f"{source.get('chain') or ''}:{source.get('symbol') or ''}"
            f"→{destination.get('chain') or ''}:{destination.get('symbol') or ''}"
        ),
        "data": {
            "intent": "swap",
            "source": dict(source),
            "destination": dict(destination),
        },
    }


def _swap_entry_chip(kind: str, language: str | None) -> dict[str, Any]:
    """The swap card's entry chips: pick another pair or go cross-chain."""
    if kind == "cross_chain":
        return {
            "type": "action",
            "label": "跨链换" if language != "en" else "Cross-chain swap",
            "message": "我想跨链兑换。" if language != "en" else "I want a cross-chain swap.",
            "value": "swap_cross_chain",
            "data": {"intent": "swap"},
        }
    return {
        "type": "action",
        "label": "其他币对" if language != "en" else "Other pairs",
        "message": "我来选择其他币对。" if language != "en" else "I'll pick another pair.",
        "value": "swap_custom",
        "data": {"intent": "swap"},
    }


def _swap_context_has_accounts(wallet_context: Mapping[str, Any]) -> bool:
    context = wallet_context or {}
    return bool(context.get("wallet_accounts") or context.get("address"))


def _swap_recommendation_desired(
    message: str, rows: list[Mapping[str, Any]]
) -> bool:
    """The swap-intent card only fires for an open-ended swap request.

    A question about swap (fees, supported pairs, slippage) or a message that
    already names a full pair should not be answered with a recommendation
    card; the model keeps driving those.
    """
    text = "".join(str(message or "").lower().split())
    if not text:
        return False
    if any(
        marker in text
        for marker in (
            "?", "？", "多少", "哪些", "怎么", "如何", "吗", "支持",
            "费率", "手续费", "滑点", "是什么",
        )
    ):
        return False
    symbols = set(_COMMON_ASSET_SYMBOLS)
    for item in rows:
        asset = item.get("asset") if isinstance(item, Mapping) else None
        if isinstance(asset, Mapping):
            symbol = canonical_symbol(str(asset.get("symbol") or "")).upper()
            if symbol:
                symbols.add(symbol)
    upper = text.upper()
    found = {symbol for symbol in symbols if symbol in upper}
    return len(found) < 2


async def _react_swap_pair_suggestions(
    *,
    wallet_provider: Any,
    wallet_context: Mapping[str, Any],
    tool_payloads: list[tuple[str, dict[str, Any]]] | None = None,
    read_facts: Mapping[str, Any] | None = None,
    language: str | None = "zh",
    timeout: float = 4,
) -> list[dict[str, Any]]:
    """Figma swap-intent card: recommend pairs from the user's balance.

    Reads the freshest balance source available (this turn, cached read facts,
    App-provided balances, then a bounded live portfolio read) and returns the
    swap-pair chips plus the ``其他币对`` / ``跨链换`` entries. The live read is
    best-effort: any failure falls back to the default pairs.
    """
    rows = _swap_balance_rows(wallet_context, tool_payloads, read_facts)
    if not rows and wallet_provider is not None and _swap_context_has_accounts(wallet_context):
        try:
            observation = await query_portfolio(
                wallet_provider,
                dict(wallet_context),
                {"chain": "ALL"},
                timeout=timeout,
            )
            portfolio = (observation or {}).get("portfolio") or {}
            rows = [item for item in portfolio.get("assets") or [] if isinstance(item, Mapping)]
        except Exception:
            rows = []
    pairs = _swap_recommended_pairs(rows)
    if not pairs:
        pairs = [tuple(dict(item) for item in pair) for pair in _SWAP_DEFAULT_PAIRS]
    suggestions = [
        _swap_pair_chip(source, destination, language) for source, destination in pairs
    ]
    suggestions.append(_swap_entry_chip("custom", language))
    suggestions.append(_swap_entry_chip("cross_chain", language))
    return suggestions


def _react_turn_tool_payloads(state: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Tool observations in the current turn (after the latest user message)."""
    messages = list(state.get("messages") or [])
    current_user_index = max(
        (
            index
            for index, message in enumerate(messages)
            if isinstance(message, HumanMessage)
        ),
        default=-1,
    )
    turn_messages = messages[current_user_index + 1 :] if current_user_index >= 0 else messages
    tool_payloads: list[tuple[str, dict[str, Any]]] = []
    for message in turn_messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            payload = json.loads(str(message.content))
        except (TypeError, ValueError):
            continue
        if isinstance(payload, Mapping):
            tool_payloads.append((str(message.name or ""), dict(payload)))
    return tool_payloads


_CHAIN_ADDRESS_PROBES = ("ETH", "BSC", "TRON", "SOLANA", "BTC")


def _react_transfer_supported_chains(state: Mapping[str, Any]) -> list[str]:
    """Chains the App declared, falling back to the known native-chain set."""
    context = state.get("wallet_context") or {}
    networks = context.get("supported_networks")
    if isinstance(networks, list) and networks:
        return [str(item) for item in networks]
    return list(_NATIVE_ASSET_DECIMALS)


def _react_transfer_context(state: Mapping[str, Any]) -> bool:
    """Whether the conversation is clearly inside a transfer-collection flow."""
    request = state.get("request") or {}
    metadata = request.get("metadata") if isinstance(request, Mapping) else None
    suggestion = (
        metadata.get("suggestion_data")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("suggestion_data"), Mapping)
        else None
    )
    if isinstance(suggestion, Mapping) and suggestion.get("intent") == "transfer":
        return True
    for msg in state.get("messages") or []:
        text = str(getattr(msg, "content", "") or "")
        if isinstance(msg, HumanMessage):
            if _looks_like_transfer_message(text):
                return True
        elif isinstance(msg, AIMessage):
            if "收款地址" in text or "输入地址" in text:
                return True
    return False


def _react_transfer_collection(
    state: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any]] | None:
    """Reconstruct a transfer collection step deterministically.

    The model sometimes answers a collection step (recipient → chain → coin →
    amount) with prose instead of calling ``transfer_quick_replies``, and the
    App renders cards from suggestions, not prose. Return the next missing slot
    and a draft so ``finalize_response`` can attach the right card; ``None``
    when no transfer collection card is due.
    """
    slots: dict[str, Any] = {}
    chain_names = {
        canonical_chain(str(chain)) for chain in _react_transfer_supported_chains(state)
    }
    request = state.get("request") or {}
    metadata = request.get("metadata") if isinstance(request, Mapping) else None
    suggestion = (
        metadata.get("suggestion_data")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("suggestion_data"), Mapping)
        else None
    )
    if isinstance(suggestion, Mapping):
        if suggestion.get("recipient"):
            slots["recipient"] = str(suggestion["recipient"]).strip()
        if suggestion.get("chain"):
            slots["chain"] = canonical_chain(str(suggestion["chain"]))
        if suggestion.get("symbol"):
            slots["symbol"] = canonical_symbol(str(suggestion["symbol"]))
    for msg in state.get("messages") or []:
        if not isinstance(msg, HumanMessage):
            continue
        text = str(msg.content or "").strip()
        if not text:
            continue
        if "recipient" not in slots and any(
            is_valid_address(chain, text) for chain in _CHAIN_ADDRESS_PROBES
        ):
            slots["recipient"] = text
            continue
        canonical = canonical_chain(text)
        if "chain" not in slots and canonical in chain_names:
            slots["chain"] = canonical
            continue
        if (
            "symbol" not in slots
            and "recipient" in slots
            and "chain" in slots
            and canonical not in chain_names
        ):
            symbol = canonical_symbol(text)
            # A numeric token is a mistyped amount, not a coin: only a
            # letter-led ticker may fill the symbol slot (App-side report: a
            # stray "0" was surfacing as the amount card's coin).
            if _looks_like_asset_symbol(symbol):
                slots["symbol"] = symbol
    if not slots.get("recipient") or not _react_transfer_context(state):
        return None
    draft: dict[str, Any] = {"recipient": slots["recipient"]}
    if slots.get("chain"):
        draft["chain"] = slots["chain"]
    if slots.get("symbol"):
        draft["symbol"] = slots["symbol"]
    if not slots.get("chain"):
        return ["transfer_chain"], draft
    # Coin unknown: the amount card needs a symbol to anchor its balance
    # presets, so the amount slot is "missing" and transfer_suggestions will
    # offer the coin card instead.
    return ["transfer_amount"], draft


def _react_transfer_user_symbol(state: Mapping[str, Any]) -> str:
    """The coin the user actually chose, not one the model defaulted.

    A coin counts as chosen only when the App echoes a tapped card's
    ``suggestion_data`` (which carries ``symbol``) or the user typed a bare coin
    into the conversation. The model filling the chain's native coin into a
    keypad is not a user choice and must not skip the coin-selection step.
    """
    request = state.get("request") or {}
    metadata = request.get("metadata") if isinstance(request, Mapping) else None
    suggestion = (
        metadata.get("suggestion_data")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("suggestion_data"), Mapping)
        else None
    )
    if isinstance(suggestion, Mapping) and suggestion.get("symbol"):
        return canonical_symbol(str(suggestion["symbol"]))
    collection = _react_transfer_collection(state)
    if collection is not None and collection[1].get("symbol"):
        return canonical_symbol(str(collection[1]["symbol"]))
    return ""


def _react_swap_selected(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """The source coin of the swap pair the user tapped, if any.

    The App echoes a tapped ``swap_pair`` card back as
    ``metadata.suggestion_data = {intent: "swap", source: {...}, destination: {...}}``.
    Return the ``source`` descriptor (chain/symbol plus optional
    address/decimals/logo) so an amount keypad can be anchored to the exact
    asset; ``None`` when the turn is not a swap-pair selection.
    """
    request = state.get("request") or {}
    metadata = request.get("metadata") if isinstance(request, Mapping) else None
    suggestion = (
        metadata.get("suggestion_data")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("suggestion_data"), Mapping)
        else None
    )
    if (
        isinstance(suggestion, Mapping)
        and str(suggestion.get("intent") or "").strip().lower() == "swap"
        and isinstance(suggestion.get("source"), Mapping)
    ):
        return dict(suggestion["source"])
    return None


def _react_swap_keypad_slots(
    keypad_slots: Mapping[str, Any],
    state: Mapping[str, Any],
    swap_intent: bool,
) -> dict[str, Any] | None:
    """Slots for an amount keypad inside a swap flow, or ``None`` otherwise.

    A swap amount step keeps the keypad (the App renders it as the swap amount
    card) and enriches its slots with ``intent: "swap"`` plus, when the user
    tapped a pair card, the source asset's token address/decimals. A transfer
    keypad is not handled here — the transfer-specific reconciliation in
    ``finalize_response`` owns that path.
    """
    slots = dict(keypad_slots)
    if str(slots.get("intent") or "").strip().lower() == "swap":
        slots.setdefault("intent", "swap")
        return slots
    if not swap_intent:
        return None
    selected = _react_swap_selected(state)
    if selected is None and not str(slots.get("symbol") or "").strip():
        # No tapped pair and no coin at all: the keypad cannot anchor an amount.
        return None
    slots.setdefault("intent", "swap")
    if not slots.get("chain") and selected and selected.get("chain"):
        slots["chain"] = str(selected["chain"])
    if not slots.get("symbol") and selected and selected.get("symbol"):
        slots["symbol"] = str(selected["symbol"])
    if selected:
        if "token_address" not in slots and selected.get("address"):
            slots["token_address"] = str(selected["address"])
        if "decimals" not in slots and selected.get("decimals") is not None:
            slots["decimals"] = int(selected["decimals"])
    return slots


def _react_swap_pair_from_message(message: str) -> tuple[str, str] | None:
    """Extract (source_symbol, destination_symbol) from an explicit swap request.

    Understands the phrasing the swap card and users emit — “把 X 换成 Y”,
    “用 X 换 Y”, “X 兑 Y”, “swap X for/to Y”, “exchange X to Y” — and returns
    canonical symbols, or ``None`` when no explicit pair is named.
    """
    compact = "".join(str(message or "").split()).lower()
    if not compact:
        return None
    match = re.search(
        r"(?:把|将|用|拿)?([a-z][a-z0-9]{1,10})"
        r"(?:换成|换为|兑成|兑换成|兑换|换|兑)"
        r"([a-z][a-z0-9]{1,10})",
        compact,
    )
    if match:
        source, destination = match.group(1), match.group(2)
        if source != destination:
            return canonical_symbol(source), canonical_symbol(destination)
    match = re.search(
        r"(?:swap|exchange)\s+([a-z][a-z0-9]{1,10})\s+(?:to|for)\s+([a-z][a-z0-9]{1,10})",
        str(message or "").lower(),
    )
    if match:
        source, destination = match.group(1), match.group(2)
        if source != destination:
            return canonical_symbol(source), canonical_symbol(destination)
    return None


def _react_swap_pinned_chain(state: Mapping[str, Any]) -> str | None:
    """The source chain the user pinned by tapping a chain chip, if any.

    The App echoes a tapped ``type:"chain"`` swap chip as
    ``metadata.suggestion_data = {intent: "swap", chain, symbol, ...}``. A pinned
    chain means the ambiguity is already resolved, so the chain-selection card
    must not be offered again.
    """
    request = state.get("request") or {}
    metadata = request.get("metadata") if isinstance(request, Mapping) else None
    suggestion = (
        metadata.get("suggestion_data")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("suggestion_data"), Mapping)
        else None
    )
    if (
        isinstance(suggestion, Mapping)
        and str(suggestion.get("intent") or "").strip().lower() == "swap"
    ):
        chain = suggestion.get("chain")
        if chain:
            return canonical_chain(str(chain))
    return None


def _react_swap_continuation(message: str) -> bool:
    """Whether a bare message confirms an in-progress swap pair.

    “兑换” / “继续兑换” / “换” / “继续” continue the pair already named earlier in
    the conversation; “我要兑换” / “我想换成什么” open a NEW swap and must not reuse
    an old pair. A message that names a symbol carries its own pair, so it is not
    a continuation by itself.
    """
    text = "".join(str(message or "").lower().split())
    if not text:
        return False
    if re.search(r"[a-z][a-z0-9]{1,10}", text):
        return False
    if any(
        marker in text
        for marker in (
            "我要", "我想", "想兑换", "想换", "帮我", "准备", "打算",
            "怎么", "如何", "可以", "吗", "？", "?", "什么",
        )
    ):
        return False
    return any(token in text for token in ("兑换", "换", "swap", "exchange", "继续"))


def _react_established_swap_pair(state: Mapping[str, Any]) -> tuple[str, str] | None:
    """The swap pair named in an earlier turn, before the current request.

    Lets a bare “兑换” confirmation keep the pair the user already picked instead
    of bouncing back to the recommended-pair card. Only pairs from prior user
    turns count; the current request's own message is excluded.
    """
    current = str(
        (state.get("request") or {}).get("message") or state.get("message") or ""
    ).strip()
    for message in reversed(list(state.get("messages") or [])):
        if not isinstance(message, HumanMessage):
            continue
        text = str(message.content or "").strip()
        if not text or text == current:
            continue
        pair = _react_swap_pair_from_message(text)
        if pair is not None:
            return pair
    return None


def _swap_source_chain_rows(
    wallet_context: Mapping[str, Any],
    tool_payloads: list[tuple[str, dict[str, Any]]] | None,
    read_facts: Mapping[str, Any] | None,
    source_symbol: str,
) -> dict[str, dict[str, Any]]:
    """Per-chain descriptors for the swap source coin the user actually holds.

    Keyed by canonical chain name; each descriptor carries ``chain``/``symbol``
    plus, when known, ``token_address``/``decimals`` so a tapped chain chip can
    pin the exact asset. Only non-empty holdings count (a coin with zero
    balance on a network cannot be the source of a swap).
    """
    symbol = canonical_symbol(source_symbol)
    rows = _swap_balance_rows(wallet_context, tool_payloads, read_facts)
    per_chain: dict[str, dict[str, Any]] = {}
    for item in rows:
        asset = item.get("asset") if isinstance(item, Mapping) else None
        if not isinstance(asset, Mapping):
            continue
        chain = canonical_chain(str(asset.get("chain") or ""))
        if not chain or canonical_symbol(str(asset.get("symbol") or "")) != symbol:
            continue
        descriptor = {"chain": chain, "symbol": symbol}
        if asset.get("address"):
            descriptor["token_address"] = str(asset["address"])
        if asset.get("decimals") is not None:
            descriptor["decimals"] = int(asset["decimals"])
        per_chain.setdefault(chain, descriptor)
    return per_chain


_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)"
)
_MARKDOWN_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
#: Option blocks the model sometimes dumps into prose instead of leaving them
#: to the structured ``suggestions`` field (``<suggestions>[...]</suggestions>``
#: and the like). Removed wholesale in ``_plain_text_message``.
_OPTION_BLOCK_RE = re.compile(
    r"<\s*(suggestions?|options?|choices?|selects?)\b[^>]*>.*?"
    r"<\s*/\s*\1\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _markdown_cells(line: str) -> list[str]:
    """Split a Markdown table row into trimmed cells."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _plain_text_message(content: str) -> str:
    """Reduce a model answer to the plain text the App can render.

    The App renders ``message`` as a short plain-text sentence and coin icons /
    chips arrive as structured fields, never as inline Markdown. The model
    sometimes embeds Markdown images (``![ETH](https://…/logo.png)``) or tables
    anyway, and occasionally dumps the structured suggestions back into prose
    (``<suggestions>[...]</suggestions>``). Strip image syntax down to its alt
    text, drop option blocks, and flatten any table so the client never has to
    size a runaway image, style a table it does not render, or display option
    JSON as chat text.
    """
    if not content:
        return content
    text = _MARKDOWN_IMAGE_RE.sub(
        lambda match: (match.group(1) or "").strip(), content
    )
    text = _OPTION_BLOCK_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = text.splitlines()
    flattened: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _MARKDOWN_TABLE_ROW_RE.match(line):
            flattened.append(line)
            index += 1
            continue
        block = [line]
        index += 1
        while index < len(lines) and _MARKDOWN_TABLE_ROW_RE.match(lines[index]):
            block.append(lines[index])
            index += 1
        for row in block:
            if _MARKDOWN_TABLE_SEPARATOR_RE.match(row):
                continue
            cells = _markdown_cells(row)
            flattened.append("，".join(cells) if cells else row)
    return "\n".join(flattened).strip()


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
    asset = _asset_with_logo(price_asset or balance_asset) or dict(price_asset or balance_asset)
    amount = (selected_balance or {}).get("amount")
    usd_price = (selected_price or {}).get("usd_price")
    response: dict[str, Any] = {
        "kind": "asset_value",
        "asset": asset,
        "chain_logo_url": chain_logo_url(str(asset.get("chain") or "")),
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
    selected: Mapping[str, Any],
    *,
    slippage_bps: int | None = None,
    recipient_address: str | None = None,
) -> dict[str, Any]:
    # The confirmation card renders the same asset rows as the quote list: the
    # coin's icon on the asset and the chain badge next to it.
    source_asset = _asset_with_logo(selected.get("source_asset"))
    destination_asset = _asset_with_logo(selected.get("destination_asset"))
    summary = {
        "provider": selected.get("provider"),
        "provider_reference": selected.get("provider_reference"),
        "source_asset": source_asset,
        "destination_asset": destination_asset,
        "source_chain_logo_url": chain_logo_url(
            str((source_asset or {}).get("chain") or "")
        ),
        "destination_chain_logo_url": chain_logo_url(
            str((destination_asset or {}).get("chain") or "")
        ),
        "input_amount": selected.get("input_amount"),
        "expected_output": selected.get("expected_output"),
        "slippage_bps": (
            slippage_bps
            if slippage_bps is not None
            else (selected.get("slippage_bps") if selected.get("slippage_bps") is not None else 100)
        ),
    }
    if recipient_address:
        # Where the destination asset lands is the one thing worth checking
        # before approving, so the confirmation must carry it.
        summary["recipient_address"] = recipient_address
    # The confirmation card also renders the fee breakdown / estimates. These
    # are display-derived and read straight off the enriched quote so the App
    # does not have to recompute (and possibly pick the wrong asset) itself.
    summary["usd_input_value"] = selected.get("usd_input_value")
    summary["usd_expected_output"] = selected.get("usd_expected_output")
    summary["network_fee"] = selected.get("network_fee")
    summary["provider_fee"] = selected.get("provider_fee")
    summary["reference_rate"] = selected.get("reference_rate")
    summary["total_payment"] = selected.get("total_payment")
    summary["eta"] = selected.get("eta") or _provider_eta_estimate(
        selected.get("provider")
    )
    return summary


_PROVIDER_ETA_ESTIMATES = {
    "bridgers": "约 1-2 分钟",
    "omnibridge": "约 1-2 分钟",
}


def _provider_eta_estimate(provider: Any) -> str | None:
    """Operational display estimate for providers that do not disclose an ETA.

    These are SLA-style labels, not fabricated on-chain data; they exist so the
    confirmation card can render 预计完成时间 even when the provider keeps it
    opaque. Providers that do report a value always win via ``quote.eta``.
    """
    value = str(provider or "").lower()
    return _PROVIDER_ETA_ESTIMATES.get(value)


def _snapshot_price(snapshots: Any, asset: Mapping[str, Any]) -> Decimal | None:
    """Best-effort USD price for an asset from a quote's price snapshots."""
    chain = str(asset.get("chain") or "").upper()
    symbol = str(asset.get("symbol") or "").upper()
    address = str(asset.get("address") or "").lower()
    if isinstance(snapshots, Mapping):
        rows = list(snapshots.values())
    elif isinstance(snapshots, list):
        rows = snapshots
    else:
        rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        candidate = row.get("asset") or row
        if not isinstance(candidate, Mapping):
            continue
        if (
            str(candidate.get("chain") or "").upper() == chain
            and str(candidate.get("symbol") or "").upper() == symbol
            and str(candidate.get("address") or "").lower() == address
        ):
            try:
                return Decimal(str(row.get("usd_price") or 0))
            except InvalidOperation:
                return None
    return None


def _swap_recipient_address(
    state: Mapping[str, Any], task: Mapping[str, Any] | None = None
) -> str | None:
    """The destination address the provider will be told to send to, if known.

    Read from the same place the prepare step reads it, so the confirmation
    shows exactly the address the order carries.
    """
    request = state.get("swap_request")
    recipient = request.get("recipient_address") if isinstance(request, Mapping) else None
    if not recipient:
        slots = (task or {}).get("slots")
        if isinstance(slots, Mapping):
            recipient = slots.get("recipient_address")
    value = str(recipient or "").strip()
    return value or None


def _swap_preflight_recipient(
    prepared: Any, request: Mapping[str, Any], chain: str
) -> str:
    """The source-chain address a swap preflight should validate as payer.

    A provider deposit transaction pays a source-chain address, exposed as the
    display ``deposit_address`` or the transaction ``to``. The swap request also
    carries ``recipient_address``, which on a cross-chain swap is the
    *destination*-chain payout address (e.g. a BTC address on an ETH-source
    swap) and must never be fed to the source-chain preflight. It is only used
    when it is itself a valid source-chain address (a same-chain swap with no
    deposit concept).
    """
    display = dict(getattr(prepared, "display", None) or {})
    candidates = (
        str(display.get("deposit_address") or "").strip(),
        str(getattr(prepared, "to", "") or "").strip(),
        str(request.get("recipient_address") or "").strip(),
    )
    for candidate in candidates:
        if candidate and is_valid_address(str(chain or ""), candidate):
            return candidate
    return ""


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
        _swap_confirmation_summary(
            selected,
            slippage_bps=task_slippage,
            recipient_address=_swap_recipient_address(state, task),
        )
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


def _balance_row_with_chain_logo(dumped: Any) -> Any:
    """Balance row carries its chain logo next to the asset logo."""
    if not isinstance(dumped, Mapping):
        return dumped
    asset = dumped.get("asset")
    chain = asset.get("chain") if isinstance(asset, Mapping) else None
    if not chain:
        return dumped
    row = dict(dumped)
    if "chain_logo_url" not in row:
        row["chain_logo_url"] = chain_logo_url(chain)
    if isinstance(asset, Mapping):
        # Native coins have no provider logo, so the chain logo stands in; a
        # token whose logo no source supplied stays null for the App placeholder.
        row["asset"] = {
            **asset,
            "logo_url": token_logo_url(
                str(chain), str(asset.get("symbol") or ""), fallback=asset.get("logo_url")
            ),
        }
    return row


def _quote_with_chain_logos(dumped: Mapping[str, Any]) -> dict[str, Any]:
    """A quote carries the source/destination chain logos next to the assets."""
    result = dict(dumped)
    source = result.get("source_asset") or {}
    destination = result.get("destination_asset") or {}
    if isinstance(source, Mapping) and source.get("chain"):
        result["source_chain_logo_url"] = chain_logo_url(source["chain"])
    if isinstance(destination, Mapping) and destination.get("chain"):
        result["destination_chain_logo_url"] = chain_logo_url(destination["chain"])
    return result


def _asset_with_logo(value: Any) -> dict[str, Any] | None:
    """Copy an asset with its icon filled in.

    A provider logo wins; a native coin reuses its chain logo so every card that
    shows the asset (quote rows, confirmation summary) has the same field.
    """
    if not isinstance(value, Mapping):
        return None
    asset = dict(value)
    chain = asset.get("chain")
    if chain:
        asset["logo_url"] = token_logo_url(
            str(chain),
            str(asset.get("symbol") or ""),
            fallback=(
                str(asset.get("logo_url")) if asset.get("logo_url") else None
            ),
        )
    return asset


def _price_with_logos(value: Any) -> Any:
    """A price (or market) row with the asset's icon and chain badge attached.

    Price rows are the one asset payload the price providers build themselves, so
    their ``asset.logo_url`` is always empty; filling it here keeps the market
    cards on the same convention as the balance rows.
    """
    row = _dump(value)
    if not isinstance(row, Mapping):
        return row
    dumped = dict(row)
    asset = _asset_with_logo(dumped.get("asset"))
    if asset is None:
        return dumped
    chain = str(asset.get("chain") or "")
    return {
        **dumped,
        "asset": asset,
        "chain_logo_url": chain_logo_url(chain) if chain else None,
    }


def _swap_status_logo_context(state: Mapping[str, Any]) -> dict[str, Any]:
    """Chain and asset identity for the swap-status card, when the order knows it.

    The status payload is built from several different states (broadcast only,
    prepared, polled order), so this reads whichever of them still carries the
    request's assets instead of expecting one fixed shape.
    """
    selected = _mapping(state.get("selected_quote"))
    pending = _mapping(state.get("pending_transaction"))
    request = _mapping(state.get("request"))
    source = _mapping(selected.get("source_asset")) or _mapping(pending.get("source_asset"))
    destination = _mapping(selected.get("destination_asset")) or _mapping(
        pending.get("destination_asset")
    )
    chain = (
        pending.get("chain")
        or source.get("chain")
        or request.get("chain")
        or _mapping(state.get("wallet_context")).get("chain")
    )
    context: dict[str, Any] = {}
    if chain:
        context["chain"] = str(chain)
        context["chain_logo_url"] = chain_logo_url(str(chain))
    if source:
        context["asset"] = _asset_with_logo(source)
    if destination:
        context["destination_asset"] = _asset_with_logo(destination)
    return context


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
) -> list[dict[str, Any]]:
    """Offer explicit replies that fill slots without taking an action for the user."""
    suggestions: list[dict[str, Any]] = []
    source_symbol = str(draft.get("source_symbol") or "").strip()
    destination_symbol = str(draft.get("destination_symbol") or "").strip()
    source_chain = str(draft.get("source_chain") or "").strip()
    destination_chain = str(draft.get("destination_chain") or "").strip()
    wallet_chain = str((wallet_context or {}).get("chain") or "").strip()

    if wallet_chain and all(field in missing for field in ("source_chain", "destination_chain")):
        suggestions.append(
            {
                "label": f"都在 {wallet_chain} 网络",
                "message": f"都在 {wallet_chain} 链",
                "type": "chain",
                "value": wallet_chain,
                **asset_logo_fields(wallet_chain, source_symbol or destination_symbol),
            }
        )
    elif "source_chain" in missing and destination_chain:
        suggestions.append(
            {
                "label": f"来源也在 {destination_chain} 网络",
                "message": f"来源也在 {destination_chain} 链",
                "type": "chain",
                "value": destination_chain,
                **asset_logo_fields(destination_chain, source_symbol),
            }
        )
    elif "destination_chain" in missing and source_chain:
        suggestions.append(
            {
                "label": f"目标也在 {source_chain} 网络",
                "message": f"目标也在 {source_chain} 链",
                "type": "chain",
                "value": source_chain,
                **asset_logo_fields(source_chain, destination_symbol),
            }
        )
    if "source_symbol" in missing and destination_symbol:
        suggestions.append(
            {
                "label": f"示例：用 USDC 换 {destination_symbol}",
                "message": "用当前网络上的 USDC 换",
                "type": "symbol",
                "value": "USDC",
            }
        )
    if "destination_symbol" in missing and source_symbol:
        suggestions.append(
            {
                "label": f"示例：把 {source_symbol} 换成 USDT",
                "message": "换成当前网络上的 USDT",
                "type": "symbol",
                "value": "USDT",
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
            amount_value: str | None = minimums[0]
        else:
            message = f"请输入 {source_symbol} 数量"
            label = f"填写 {source_symbol} 数量"
            amount_value = None
        suggestions.append(
            {"label": label, "message": message, "type": "amount", "value": amount_value}
        )
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
        "BTC": "BTC",
        "BITCOIN": "BTC",
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
    "BTC": 8,
    "ETH": 18,
    "BASE": 18,
    "ARBITRUM": 18,
    "OPTIMISM": 18,
    "BSC": 18,
    "POLYGON": 18,
    "TRON": 6,
    "SOLANA": 9,
}


def _transfer_coin_choices(
    context: Mapping[str, Any], chain: str
) -> list[dict[str, Any]]:
    """Native coin plus the wallet's token assets on ``chain``, for the coin card.

    The amount card anchors its 25/50/75/100% presets to one asset's balance, so
    it is meaningless before the user picks the coin. This returns the chain's
    native coin first, then the token assets the App reported for the chain, so
    the coin card can ask for exactly that.
    """
    canonical = canonical_chain(str(chain or ""))
    native = _native_symbol(str(chain or ""), dict(context))
    choices: list[dict[str, Any]] = []
    seen: set[str] = set()
    if native:
        seen.add(native.upper())
        choices.append(
            {
                "symbol": native,
                "token_address": None,
                "decimals": _NATIVE_ASSET_DECIMALS.get(
                    canonical, native_decimals(canonical)
                ),
                # The native coin's icon is its chain logo; the caller resolves
                # it through ``asset_logo_fields``.
                "logo_url": None,
            }
        )
    for raw_balance in context.get("token_balances") or context.get("assets") or []:
        item = raw_balance.get("asset") if isinstance(raw_balance, Mapping) else None
        if not isinstance(item, Mapping):
            continue
        if canonical_chain(str(item.get("chain") or "")) != canonical:
            continue
        address = str(item.get("address") or "").strip()
        if not address:
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        choices.append(
            {
                "symbol": symbol,
                "token_address": address,
                "decimals": item.get("decimals"),
                "logo_url": item.get("logo_url"),
            }
        )
    return choices


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


def _declared_accounts(wallet_context: Mapping[str, Any] | None, chain: str) -> list[str]:
    """Every address the App declared for ``chain``, in publish order.

    Falls back to the single connected account only when the App published no
    account for that chain, so an EVM account is never reused on another family.
    """
    context = wallet_context or {}
    canonical = canonical_chain(str(chain or ""))
    accounts = context.get("wallet_accounts")
    addresses: list[str] = []
    if isinstance(accounts, list):
        for item in accounts:
            if not isinstance(item, Mapping):
                continue
            if canonical_chain(str(item.get("chain") or "")) != canonical:
                continue
            address = str(item.get("address") or "").strip()
            if address and address not in addresses and is_valid_address(canonical, address):
                addresses.append(address)
    if addresses:
        return addresses
    legacy = str(context.get("address") or "").strip()
    if legacy and is_valid_address(canonical, legacy):
        return [legacy]
    return []


def _chain_sender(wallet_context: Mapping[str, Any] | None, chain: str) -> str | None:
    """Resolve the transfer sender for a chain from the App's account list."""
    accounts = _declared_accounts(wallet_context, chain)
    return accounts[0] if accounts else None


def _swap_address_for_chain(
    wallet_context: Mapping[str, Any] | None, chain: str, current: str | None
) -> str:
    """Keep an address that matches ``chain``, else use the App's account for it.

    A BTC-source swap quoted with the browser wallet's EVM account produced a
    quote that the provider later rejected as ``invalid address`` on the swap
    step, and a BTC address used as the USDT(ETH) receiver was rejected the same
    way. Prefer the account the App declared for the chain, and keep the given
    value when it declared none: an unknown family must not block the quote.
    """
    value = str(current or "").strip()
    if not chain or not value or is_valid_address(chain, value):
        return value
    return _chain_sender(wallet_context, chain) or value


def _utxo_source_addresses(
    wallet_context: Mapping[str, Any] | None, chain: str, primary: str | None = None
) -> list[str]:
    """Every spendable address of a UTXO chain, with the primary sender first."""
    canonical = canonical_chain(str(chain or ""))
    ordered: list[str] = []
    for candidate in (primary, *_declared_accounts(wallet_context, canonical)):
        address = str(candidate or "").strip()
        if address and address not in ordered and is_valid_address(canonical, address):
            ordered.append(address)
    return ordered


def _btc_swap_source_options(
    wallet_context: Mapping[str, Any] | None, chain: str
) -> list[str]:
    """Declared BTC addresses offered as a swap 出金 (source) address."""
    canonical = canonical_chain(str(chain or ""))
    if canonical not in {"BTC", "BITCOIN"}:
        return []
    return _utxo_source_addresses(wallet_context, canonical)


def _pinned_swap_provider(
    state: Mapping[str, Any], providers: Mapping[str, Any]
) -> str | None:
    """The swap provider the App explicitly pinned via suggestion_data.

    ``metadata.suggestion_data = {intent: "swap", provider: "bridgers"}`` lets
    the App drive the channel (Lif: 测试 Bridgers 完整授权流程需要能指定渠道).
    The pin is persisted on the swap ``active_task``, so the App only needs to
    send it once (e.g. the coin-selection turn); later amount/quote turns that
    omit ``provider`` keep quoting/preparing through the pinned channel.
    Returns the canonical provider key when the pinned name matches an enabled
    provider; otherwise ``None`` so normal automatic selection is unchanged.
    """
    explicit = _suggestion_pinned_provider(state, providers)
    if explicit is not None:
        return explicit
    # Cross-turn fallback: the App pinned the channel on an earlier turn of the
    # same swap task and this turn did not repeat it. Only an ongoing swap task
    # keeps the pin; a new transfer/swap task starts unpinned.
    active_task = state.get("active_task")
    if not isinstance(active_task, Mapping):
        return None
    if str(active_task.get("kind") or "") != "swap":
        return None
    if active_task.get("status") in {"completed", "cancelled"}:
        return None
    persisted = str(active_task.get("pinned_provider") or "").strip().lower()
    if not persisted:
        return None
    for key in providers:
        if str(key).strip().lower() == persisted:
            return str(key)
    return None


def _suggestion_pinned_provider(
    state: Mapping[str, Any], providers: Mapping[str, Any]
) -> str | None:
    """The provider pinned on this turn's ``suggestion_data``, if any.

    ``metadata.suggestion_data = {provider: "bridgers"}`` (optionally carrying
    ``intent: "swap"``) lets the App drive the channel. The intent check is
    intentionally loose — the App sometimes echoes only ``provider`` — but a
    strong non-swap intent (e.g. ``transfer``) never pins through this path.
    """
    request = _mapping(state.get("request"))
    metadata = request.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    suggestion = metadata.get("suggestion_data")
    if not isinstance(suggestion, Mapping):
        return None
    pinned = str(suggestion.get("provider") or "").strip().lower()
    if not pinned:
        return None
    intent = str(suggestion.get("intent") or "").strip().lower()
    if intent and intent not in {"swap", "swap_quote"}:
        return None
    for key in providers:
        if str(key).strip().lower() == pinned:
            return str(key)
    return None


def _swap_requires_btc_source_selection(
    draft: Mapping[str, Any] | None, wallet_context: Mapping[str, Any] | None
) -> list[str] | None:
    """The BTC addresses to offer when a UTXO-source swap needs an explicit 出金地址.

    A BTC swap's provider-facing fromAddress/userAddr must be a single address
    the user picked (Lif: “出金的地址只能从中间选一个”). When the draft carries
    no explicit valid BTC sender yet and the wallet declared more than one BTC
    address, ask the App which one to use instead of silently picking the first.
    Returns ``None`` when no address choice is due.
    """
    chain = canonical_chain(str((draft or {}).get("source_chain") or ""))
    if chain not in {"BTC", "BITCOIN"}:
        return None
    sender = str((draft or {}).get("sender_address") or "").strip()
    if sender and is_valid_address(chain, sender):
        return None
    addresses = _btc_swap_source_options(wallet_context, chain)
    if len(addresses) < 2:
        return None
    return addresses


async def _adapter_balance_rows(adapter: Any, addresses: Sequence[str]) -> list[Any]:
    """Native balance pooled across ``addresses``, plus any token balances.

    A UTXO wallet is spread over many addresses, so reading only the connected
    one understates it. EVM-style chains pass a single address and are unchanged.
    """
    ordered = [str(item).strip() for item in addresses if str(item).strip()]
    native: Any = None
    total_raw = 0
    for address in ordered:
        balance = await adapter.get_native_balance(address)
        if native is None:
            native = balance
        total_raw += int(balance.amount_raw)
    rows: list[Any] = []
    if native is not None:
        if total_raw != int(native.amount_raw):
            native = native.model_copy(update={"amount_raw": str(total_raw)})
        rows.append(native)
    if hasattr(adapter, "get_token_balances"):
        for address in ordered:
            rows.extend(await adapter.get_token_balances(address))
    return rows


def _utxo_signing_info(
    wallet_context: Mapping[str, Any] | None, chain: str
) -> dict[str, dict[str, Any]]:
    """Per-address signing material the App reported for a UTXO chain.

    Only public data: a compressed pubkey plus the derivation path and master
    fingerprint a BIP174 signer needs. Nothing here can spend on its own.
    """
    canonical = canonical_chain(str(chain or ""))
    info: dict[str, dict[str, Any]] = {}
    for item in (wallet_context or {}).get("wallet_accounts") or []:
        if not isinstance(item, Mapping):
            continue
        if canonical_chain(str(item.get("chain") or "")) != canonical:
            continue
        address = str(item.get("address") or "").strip()
        pubkey = str(item.get("pubkey") or "").strip()
        if not address or not pubkey:
            continue
        entry: dict[str, Any] = {"pubkey": pubkey}
        for field in ("derivation_path", "master_fingerprint"):
            value = str(item.get(field) or "").strip()
            if value:
                entry[field] = value
        info[address] = entry
    return info


_ZERO_EVM_ADDRESS = "0x" + "0" * 40

# UI surfaces the App owns. The agent may ask for one, but never collects the
# data inside them itself.
_CLIENT_ACTION_TYPES = (
    "open_contacts",
    "scan_qr",
    "open_amount_keypad",
    "address_input",
    "select_btc_address",
)


def _address_risk(
    sender: str | None,
    recipient: str | None,
    chain: str,
    wallet_context: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Deterministic recipient risk used by the address-risk card.

    Only checks this service can make honestly: a burn address, a transfer to
    the sender itself, and a transfer to another address of this same wallet.
    """
    recipient_text = str(recipient or "").strip()
    if not recipient_text:
        return None
    canonical = canonical_chain(str(chain or ""))
    lowered = recipient_text.lower()
    if canonical in EVM_ALIASES and lowered == _ZERO_EVM_ADDRESS:
        return {
            "level": "danger",
            "code": "ZERO_ADDRESS_RECIPIENT",
            "message": "收款地址是零地址，转入的资产会永久丢失，请重新核对。",
        }
    sender_text = str(sender or "").strip()
    if sender_text and lowered == sender_text.lower():
        return {
            "level": "warning",
            "code": "SELF_TRANSFER",
            "message": "收款地址与付款地址相同，除网络手续费外不会有资产转移。",
        }
    declared = {address.lower() for address in _declared_accounts(wallet_context, canonical)}
    if lowered in declared:
        return {
            "level": "warning",
            "code": "OWN_ADDRESS_RECIPIENT",
            "message": "收款地址属于本钱包自己的地址，请确认这是有意的（例如整理 UTXO）。",
        }
    return None


def _history_targets(
    wallet_context: Mapping[str, Any] | None, chain: str | None = None
) -> list[tuple[str, str]]:
    """(chain, address) pairs to query for the transaction-history card."""
    context = wallet_context or {}
    canonical = canonical_chain(str(chain or ""))
    wanted = "" if canonical == "ALL" else canonical
    targets: list[tuple[str, str]] = []
    accounts = context.get("wallet_accounts")
    if isinstance(accounts, list):
        for item in accounts:
            if not isinstance(item, Mapping):
                continue
            account_chain = canonical_chain(str(item.get("chain") or ""))
            address = str(item.get("address") or "").strip()
            if not account_chain or not address or not is_valid_address(account_chain, address):
                continue
            if wanted and account_chain != wanted:
                continue
            if (account_chain, address) not in targets:
                targets.append((account_chain, address))
    if targets:
        return targets
    legacy_chain = canonical_chain(str(context.get("chain") or ""))
    legacy = str(context.get("address") or "").strip()
    if legacy and is_valid_address(legacy_chain or wanted, legacy):
        return [(legacy_chain or wanted, legacy)]
    return []


def _history_direction(record: Mapping[str, Any], address: str) -> str:
    """Provider-declared direction wins; otherwise derive it from from/to."""
    declared = str(record.get("direction") or "").strip().lower()
    if declared in {"send", "receive", "swap"}:
        return declared
    target = str(address or "").strip().lower()
    if not target:
        return "unknown"
    if str(record.get("to_address") or "").strip().lower() == target:
        return "receive"
    if str(record.get("from_address") or "").strip().lower() == target:
        return "send"
    return "unknown"


def _transaction_history_item(record: Any, address: str) -> dict[str, Any]:
    payload = record.model_dump(mode="json") if hasattr(record, "model_dump") else dict(record)
    direction = _history_direction(payload, address)
    chain = payload.get("chain")
    symbol = payload.get("symbol")
    if direction == "receive":
        counterparty = payload.get("from_address")
    else:
        counterparty = payload.get("to_address") or payload.get("from_address")
    return {
        "chain": chain,
        "chain_logo_url": chain_logo_url(chain) if chain else None,
        "tx_hash": payload.get("tx_hash"),
        "status": payload.get("status"),
        "direction": direction,
        "symbol": symbol,
        "decimals": payload.get("decimals"),
        "logo_url": payload.get("logo_url") or token_logo_url(chain, symbol),
        "amount_raw": payload.get("value"),
        # OKX history rows carry no decimals; say so instead of letting the
        # model invent a scale for the raw integer.
        "raw_only": payload.get("decimals") is None,
        "counterparty": counterparty,
        "occurred_at": payload.get("occurred_at") or payload.get("confirmed_at"),
        "failure_reason": payload.get("failure_reason"),
        "block_number": payload.get("block_number"),
    }


def _transaction_history_payload(
    items: Sequence[Mapping[str, Any]],
    *,
    direction: str | None = None,
    chain: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Shape the fetched records into the v1.6.9 history card."""
    wanted = str(direction or "").strip().lower()
    visible = [item for item in items if not wanted or item.get("direction") == wanted]
    visible = sorted(visible, key=lambda item: str(item.get("occurred_at") or ""), reverse=True)
    selected = [dict(item) for item in visible[: max(1, limit)]]
    # The bubble's counts must describe exactly the rows the card shows
    # (``selected``), never the whole fetched set, or "最近 20 笔" fights the
    # "38 转出 + 90 入账" summary (App-side report). The fetched total is still
    # exposed as ``fetched`` and mentioned in the prose when it is larger.
    counts: dict[str, int] = {}
    for item in selected:
        key = str(item.get("direction") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    summary = "、".join(
        part
        for part in (
            f"{counts['send']} 笔转出" if counts.get("send") else "",
            f"{counts['receive']} 笔入账" if counts.get("receive") else "",
            f"{counts['swap']} 笔兑换" if counts.get("swap") else "",
        )
        if part
    )
    payload: dict[str, Any] = {
        "ok": True,
        "kind": "transaction_history",
        "title": "最近交易记录",
        "items": selected,
        "total": len(selected),
        "fetched": len(items),
        "summary": summary,
        "filter": {
            "chain": canonical_chain(str(chain or "")) or None,
            "direction": wanted or None,
        },
    }
    if not selected:
        payload["message"] = "没有查到符合条件的交易记录。"
        return payload
    confirmed = all(str(item.get("status") or "") == "confirmed" for item in selected)
    tail = "都是已确认状态。" if confirmed else "其中包含未确认的交易。"
    total_note = f"，共 {len(visible)} 笔" if len(visible) > len(selected) else ""
    payload["message"] = (
        f"最近 {len(selected)} 笔记录拉出来了 👆"
        + (f" 其中 {summary}{total_note}，" if summary else f" {total_note.lstrip('，')}，")
        + tail
        + "想看某笔详情或者筛选只看支出，跟我说一声。"
    )
    return payload


_CHAIN_DISPLAY_NAMES = {
    "ETH": "Ethereum",
    "BSC": "BNB Chain",
    "BASE": "Base",
    "POLYGON": "Polygon",
    "ARBITRUM": "Arbitrum",
    "OPTIMISM": "Optimism",
    "BTC": "Bitcoin",
    "TRON": "TRON",
    "SOLANA": "Solana",
    # Also on the design's receive card; Robinhood has no Trust Wallet entry, so
    # it stays a label with a null logo.
    "AVALANCHE": "Avalanche",
    "AVAX": "Avalanche",
    "CELO": "Celo",
    "MEGAETH": "MegaETH",
    "ROBINHOOD": "Robinhood",
}


def _receive_address_payload(wallet_context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Group the App's declared accounts into per-family receive cards.

    EVM chains share one account, so they collapse into a single card labelled
    for every EVM network; each other chain family gets its own card.
    """
    context = wallet_context or {}
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def add(raw_chain: Any, raw_address: Any) -> None:
        canonical = canonical_chain(str(raw_chain or ""))
        address = str(raw_address or "").strip()
        if not canonical or not address or not is_valid_address(canonical, address):
            return
        family = "EVM" if canonical in EVM_ALIASES else canonical
        entry = grouped.get(family)
        if entry is None:
            entry = {"group": family, "address": address, "addresses": [], "chains": []}
            grouped[family] = entry
            order.append(family)
        if address not in entry["addresses"]:
            entry["addresses"].append(address)
        if canonical not in entry["chains"]:
            entry["chains"].append(canonical)

    accounts = context.get("wallet_accounts")
    if isinstance(accounts, list):
        for item in accounts:
            if isinstance(item, Mapping):
                add(item.get("chain"), item.get("address"))
    if not order:
        add(context.get("chain"), context.get("address"))

    groups = [grouped[key] for key in order]
    # The shared EVM account leads the receive card, as in the design.
    groups.sort(key=lambda item: 0 if item["group"] == "EVM" else 1)
    for item in groups:
        if item["group"] == "EVM":
            item["label"] = "支持所有 EVM 链" if len(item["chains"]) > 1 else "EVM 网络"
        else:
            item["label"] = _CHAIN_DISPLAY_NAMES.get(item["group"], item["group"])

    supported: list[str] = []
    for item in groups:
        for chain in item["chains"]:
            if chain not in supported:
                supported.append(chain)

    payload: dict[str, Any] = {
        "ok": bool(groups),
        "kind": "receive_address",
        "groups": groups,
        "address": groups[0]["address"] if groups else None,
        "supported_chains": supported,
        "display_chains": [_CHAIN_DISPLAY_NAMES.get(chain, chain) for chain in supported],
        # The v1.6.9 card labels these chains as plain chips; the lookup lets the
        # App draw a badge for any of them without its own chain table.
        "chain_logo_urls": {chain: chain_logo_url(chain) for chain in supported},
    }
    if not groups:
        payload["code"] = "RECEIVE_ADDRESS_UNAVAILABLE"
        payload["error"] = "App 还没有提供任何公共地址，无法给出收款地址。"
        return payload
    primary = groups[0]
    if primary["group"] == "EVM":
        display = " / ".join(
            _CHAIN_DISPLAY_NAMES.get(chain, chain) for chain in primary["chains"]
        )
        payload["message"] = (
            f"你的收款地址：{primary['address']} 👆 支持 EVM 链：{display} 等，直接转就行～"
        )
    else:
        payload["message"] = f"你的 {primary['label']} 收款地址：{primary['address']} 👆"
    return payload


def _transfer_draft_request(
    draft: dict[str, Any], wallet_context: dict[str, Any] | None
) -> tuple[TransferRequest | None, list[str]]:
    context = wallet_context or {}
    normalized = dict(draft)
    normalized.setdefault("chain", normalized.get("transfer_chain") or context.get("chain"))
    symbol = normalized.get("symbol") or normalized.get("transfer_symbol")
    token_address = normalized.get("token_address") or normalized.get("transfer_token_address")
    inferred_chain = _unique_native_chain(str(symbol or ""), token_address)
    if inferred_chain and canonical_chain(str(normalized.get("chain") or "")) != inferred_chain:
        # A uniquely identifiable native asset (BTC/BNB/TRX/SOL/MATIC) pins its
        # chain, mirroring the swap slot reconciliation for the same symbols.
        normalized["chain"] = inferred_chain
        normalized["transfer_chain"] = inferred_chain
    chain = str(normalized.get("chain") or "")
    sender = next(
        (
            str(candidate).strip()
            for candidate in (normalized.get("sender"), normalized.get("transfer_sender"))
            if candidate and is_valid_address(chain, str(candidate))
        ),
        None,
    )
    # An explicit sender from another chain family (the connected EVM account is
    # the common case) must never be reused here, even when a caller passed it;
    # fall back to the per-chain account list instead.
    normalized["sender"] = sender or _chain_sender(context, chain)
    normalized.setdefault("recipient", normalized.get("transfer_recipient"))
    normalized.setdefault("amount", normalized.get("transfer_amount"))
    normalized.setdefault("amount_raw", normalized.get("transfer_amount_raw"))
    normalized.setdefault("symbol", normalized.get("transfer_symbol"))
    normalized.setdefault("token_address", normalized.get("transfer_token_address"))
    normalized.setdefault("decimals", normalized.get("transfer_decimals"))
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
            amount_decimals = (
                native_decimals(str(normalized["chain"])) if is_native else int(decimals)
            )
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

    connected = str(context.get("address") or "")
    if not sender:
        add(
            "wallet_account",
            "warning",
            "缺少发送方上下文，无法核对当前钱包账户。",
            code="CHECK_UNAVAILABLE",
        )
    elif connected and connected.lower() == sender.lower():
        add("wallet_account", "passed", "发送方与当前钱包账户一致。")
    elif connected and not is_valid_address(chain, connected):
        # The connected browser account is not even a valid address on this
        # chain family, so it can never sign here; Bitcoin/TRON/Solana app
        # accounts sign outside the browser (e.g. a PSBT handed to a Bitcoin
        # signer) and a mismatch is expected rather than an error.
        add(
            "wallet_account",
            "warning",
            f"当前连接的账户不属于 {chain} 网络；该笔交易由 App 声明的 {chain} 账户签名。",
            code="WALLET_ACCOUNT_EXTERNAL_SIGNER",
            details={"wallet_address": connected, "sender": sender},
        )
    elif connected:
        add(
            "wallet_account",
            "failed",
            "当前连接的钱包账户与交易发送方不一致。",
            code="WALLET_ACCOUNT_MISMATCH",
            details={"wallet_address": connected, "sender": sender},
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
            recipient_on_source = bool(recipient) and is_valid_address(chain, recipient)
            if not sender_valid:
                add(
                    "addresses",
                    "failed",
                    "发送方地址格式无效。",
                    code="INVALID_ADDRESS",
                    details={"sender_valid": False, "recipient_valid": recipient_on_source},
                )
            elif recipient and not recipient_on_source:
                # Cross-chain swaps carry a destination-chain payout address
                # (e.g. a BTC address on an ETH-source swap). The source-chain
                # adapter cannot validate it; keep the check non-blocking and
                # let the provider/order carry the destination-address contract.
                add(
                    "addresses",
                    "warning",
                    "发送方地址有效；收款方地址不属于本链，已跳过源链格式校验。",
                    code="CHECK_PARTIAL",
                    details={"sender_valid": True, "recipient_valid": False, "cross_chain": True},
                )
            else:
                recipient_valid = (
                    await adapter.validate_address(recipient) if recipient else True
                )
                if not recipient_valid:
                    add(
                        "addresses",
                        "failed",
                        "收款方地址格式无效。",
                        code="INVALID_ADDRESS",
                        details={"sender_valid": True, "recipient_valid": False},
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

    risk = _address_risk(sender, recipient, chain, wallet_context)
    if risk is not None:
        add(
            "address_risk",
            "failed" if risk["level"] == "danger" else "warning",
            risk["message"],
            code=risk["code"],
            details={"level": risk["level"]},
        )
    elif recipient:
        add("address_risk", "passed", "收款地址未命中已知风险。")

    if token is not None and str(token.chain).upper() != chain.upper():
        add("token_chain", "failed", "Token 所属链与交易链不一致.", code="TOKEN_CHAIN_MISMATCH")

    fee = None
    if hasattr(adapter, "estimate_fee"):
        try:
            display = dict(getattr(transaction, "display", None) or {})
            fee_kwargs: dict[str, Any] = {
                "to": transaction.to,
                "data": transaction.data,
                "from_address": sender or None,
                "value": getattr(transaction, "value", None),
            }
            # Account-model chains (TRON) carry the token contract and the EVM
            # calldata for a smart-contract call in the transaction display, not
            # in ``to``/``data`` (which hold the recipient and the node-built
            # signing payload). Pass them so the adapter can simulate the call
            # and price its energy instead of reporting the native-transfer fee.
            if display.get("contract"):
                fee_kwargs["contract"] = str(display["contract"])
            if display.get("calldata"):
                fee_kwargs["calldata"] = str(display["calldata"])
            try:
                fee = await adapter.estimate_fee(**fee_kwargs)
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

    # A UTXO builder already selected its inputs, so its fee is exact; use it
    # instead of the generic one-input estimate for the balance check and card.
    if _is_utxo_adapter(adapter) and fee is not None:
        selected_fee = str(dict(getattr(transaction, "display", None) or {}).get("fee_raw") or "")
        if selected_fee.isdigit():
            fee = fee.model_copy(
                update={
                    "amount_raw": selected_fee,
                    "amount": Decimal(selected_fee) / (Decimal(10) ** fee.asset.decimals),
                }
            )

    gas_sources: dict[str, Any] = {
        "rpc": _fee_dump(fee).get("gas_limit") if fee is not None else None,
        "okx": None,
        "simulation": None,
    }
    # Only EVM chains carry EVM calldata that OKX's gas-limit and simulation
    # endpoints can interpret. Bitcoin/UTXO transactions carry a PSBT and the
    # account-model chains (TRON, Solana) carry node-built protobuf payloads, so
    # those endpoints do not apply and are reported as unsupported.
    evm_style = getattr(adapter, "transaction_model", "evm") == "evm"
    simulation = None
    if not evm_style:
        gas_sources["okx"] = "unsupported"
        gas_sources["simulation"] = "unsupported"
    elif wallet_provider is not None:
        if not hasattr(wallet_provider, "estimate_gas_limit"):
            gas_sources["okx"] = "unavailable"
        if not hasattr(wallet_provider, "simulate_transaction"):
            gas_sources["simulation"] = "unavailable"
    if wallet_provider is not None and evm_style:
        # OKX's pre-transaction endpoints interpret ``toAddress`` as the
        # contract being called (or the payee for a native transfer). That is
        # the unsigned transaction's ``to`` — e.g. the ERC-20 token contract
        # for a token transfer — not the human-facing ``recipient`` (which for
        # a provider deposit is a separate platform contract that would revert
        # against arbitrary token calldata and produce a false preflight
        # failure).
        tx_to = str(getattr(transaction, "to", "") or recipient)
        tx_context = TransactionContext(
            chain=chain,
            from_address=sender,
            to_address=tx_to,
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
                    "Gas 预检查暂时不可用。",
                    code="OKX_GAS_UNAVAILABLE",
                )
        if hasattr(wallet_provider, "simulate_transaction"):
            try:
                simulation = await wallet_provider.simulate_transaction(tx_context)
                if simulation.gas_used is not None:
                    gas_sources["simulation"] = str(simulation.gas_used)
                if simulation.success:
                    add("simulation", "passed", "交易模拟通过。")
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
                    "交易模拟暂时不可用，签名前请在钱包中确认。",
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
            # UTXO chains spend the pooled UTXOs of every address the App
            # declared, so the balance check has to pool those addresses too.
            balance_addresses = [sender]
            if _is_utxo_adapter(adapter):
                pooled = _utxo_source_addresses(context, chain, sender)
                if len(pooled) > 1:
                    balance_addresses = pooled
            total_raw = 0
            for address in balance_addresses:
                balance = await adapter.get_native_balance(address)
                total_raw += int(balance.amount_raw)
                if native_balance is None:
                    native_balance = balance
            if native_balance is not None and total_raw != int(native_balance.amount_raw):
                native_balance = native_balance.model_copy(
                    update={"amount_raw": str(total_raw)}
                )
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
        "risk": risk,
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
    # Both sides must live on their own chain: the connected wallet address is
    # only valid for its own family, and quotes built with a foreign address are
    # rejected at the swap step rather than at the quote step.
    normalized["sender_address"] = _swap_address_for_chain(
        context,
        str(normalized.get("source_chain") or ""),
        normalized.get("sender_address") or context.get("address"),
    )
    normalized["recipient_address"] = _swap_address_for_chain(
        context,
        str(normalized.get("destination_chain") or ""),
        normalized.get("recipient_address") or context.get("address"),
    )
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
                    # The same-name-token picker shows one row per candidate, so
                    # each row carries its own icon and chain badge.
                    **asset_logo_fields(asset.chain, asset.symbol, asset.logo_url),
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

    async def _btc_source_address_options(
        addresses: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Per-address entries for the select_btc_address surface.

        Balances are best-effort per-address reads; the surface still works when
        a read fails or the adapter is unavailable (the App also holds its own
        per-address balances).
        """
        options: list[dict[str, Any]] = []
        adapter = execution_adapter("BTC")
        for address in addresses:
            entry: dict[str, Any] = {"address": str(address)}
            if adapter is not None and hasattr(adapter, "get_native_balance"):
                try:
                    balance = await adapter.get_native_balance(str(address))
                    entry["balance_raw"] = str(balance.amount_raw)
                    if balance.amount is not None:
                        entry["balance"] = str(balance.amount)
                except Exception:
                    pass
            options.append(entry)
        return options

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
        app_networks = context.get("supported_networks")
        if isinstance(app_networks, list) and app_networks:
            # The provider knows more chains than this wallet app can use; only
            # offer the networks the App actually declared, under the App's own
            # spelling, collapsing provider aliases such as ARB / ARBITRUM.
            app_label_by_canonical: dict[str, str] = {}
            for item in app_networks:
                app_label_by_canonical.setdefault(
                    canonical_chain(str(item)), str(item).upper()
                )
            scoped: list[str] = []
            for chain in supported_chains:
                label = app_label_by_canonical.get(canonical_chain(chain))
                if label is not None and label not in scoped:
                    scoped.append(label)
            if scoped:
                supported_chains = scoped
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

    def _order_supported_chains(
        chains: Any, current: Any
    ) -> list[str]:
        """Return supported chains with the wallet's current network first.

        The design's chain card leads with the current network (e.g. the
        connected chain), then lists the rest; the App renders them as the
        chain options.
        """
        ordered = [str(value) for value in (chains or [])]
        if not current:
            return ordered
        current_name = canonical_chain(str(current))
        current_label = next(
            (name for name in ordered if canonical_chain(name) == current_name),
            None,
        )
        if current_label is None:
            return ordered
        rest = [
            name for name in ordered if canonical_chain(name) != current_name
        ]
        return [current_label, *rest]

    def _transfer_wallet_asset(
        context: Mapping[str, Any], chain: Any, symbol: Any
    ) -> dict[str, Any] | None:
        """Return the unique wallet asset matching chain+symbol, if unambiguous.

        Native coins (no contract) return None; a token whose address can be
        pinned to a single (chain, symbol) pair yields its address/decimals so
        the App's amount card can identify the exact asset.
        """
        if not chain or not symbol:
            return None
        wallet_assets: list[Mapping[str, Any]] = []
        for raw_balance in (
            context.get("token_balances") or context.get("assets") or []
        ):
            item = raw_balance.get("asset") if isinstance(raw_balance, Mapping) else None
            if isinstance(item, Mapping):
                wallet_assets.append(item)
        matching = [
            item
            for item in wallet_assets
            if item.get("address")
            and canonical_chain(str(item.get("chain"))) == canonical_chain(str(chain))
            and canonical_symbol(str(item.get("symbol"))) == canonical_symbol(str(symbol))
        ]
        unique = {
            (str(item.get("address")).lower(), item.get("decimals")): item
            for item in matching
        }
        if len(unique) == 1:
            return next(iter(unique.values()))
        return None

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
        if symbol is not None and not _looks_like_asset_symbol(symbol):
            # A mistyped amount ("0") can leak into the transfer draft as the
            # symbol. Treat it as unknown so the coin card drives the step
            # instead of an amount card anchored to a "0" coin.
            symbol = None
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
            # Design v1.6.9's address-input card leads with 输入地址: the user
            # types or pastes the address and the App opens its own input
            # surface. Leading with the wallet's own address made a
            # self-transfer shortcut look like the default answer.
            suggestions.append(
                {
                    "label": "输入地址" if language == "zh" else "Enter an address",
                    "message": (
                        "我来提供收款地址。"
                        if language == "zh"
                        else "I'll provide the recipient address."
                    ),
                    "type": "action",
                    "value": "input_address",
                    # Carry the slots already collected so the App can continue
                    # this task after its address input, but never a recipient:
                    # the connected wallet must not become the default payee.
                    "data": dict(base_data),
                }
            )
            # Mirrors the design's quick row order: enter an address, pick a
            # contact (App-side), or look at the balance before continuing.
            suggestions.extend(
                [
                    {
                        "label": "转给联系人" if language == "zh" else "Pick a contact",
                        "message": (
                            "我想从联系人里选一个收款地址。"
                            if language == "zh"
                            else "I want to pick the recipient from my contacts."
                        ),
                        "type": "action",
                        "value": "pick_contact",
                        "data": {"intent": "transfer"},
                    },
                    {
                        "label": "先看看余额" if language == "zh" else "Check my balance",
                        "message": (
                            "先看看我的余额。" if language == "zh" else "Show my balance first."
                        ),
                        "type": "action",
                        "value": "show_balance",
                        "data": {"intent": "transfer"},
                    },
                ]
            )

        if "transfer_token_address" in missing or "transfer_decimals" in missing:
            asset = _transfer_wallet_asset(context, chain, symbol)
            if asset is not None:
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
                        "type": "token",
                        "value": asset.get("address"),
                        # The coin card's asset line renders the logo next to the
                        # symbol; the wallet's own asset is the best source, and
                        # a native coin falls back to its chain logo.
                        **asset_logo_fields(asset_chain, asset_symbol, asset.get("logo_url")),
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
            # Design's chain card leads with the wallet's current network and
            # lists the rest after it. Chain is the next unanswered slot, so
            # only chain options are offered on this step (address → chain →
            # amount, one card at a time).
            ordered_chains = _order_supported_chains(
                facts.get("supported_chains", []), context.get("chain")
            )
            recipient = draft.get("transfer_recipient") or draft.get("recipient")
            if recipient:
                # Only offer chains the collected recipient is actually valid
                # on (full per-chain address rules). A TRON recipient must not
                # be offered EVM networks, or the transfer would fail or land
                # on the wrong chain.
                ordered_chains = [
                    chain
                    for chain in ordered_chains
                    if is_valid_address(chain, str(recipient))
                ]
            for supported_chain in ordered_chains:
                if len(suggestions) >= 4:
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
                        "type": "chain",
                        "value": supported_chain,
                        "chain_logo_url": chain_logo_url(supported_chain),
                        "data": {**base_data, "chain": supported_chain},
                    }
                )

        if (
            "transfer_amount" in missing
            and "transfer_recipient" not in missing
            and "transfer_chain" not in missing
        ):
            if not symbol:
                # The coin is still unknown, so a percentage-of-balance amount
                # card has no balance to anchor to. Ask for the coin first
                # (address → chain → coin → amount) with the chain's native
                # coin plus the wallet's tokens on that chain.
                for coin in _transfer_coin_choices(context, chain):
                    if len(suggestions) >= 4:
                        break
                    coin_data = {
                        **base_data,
                        "chain": chain,
                        "symbol": coin["symbol"],
                    }
                    if coin.get("token_address"):
                        coin_data["token_address"] = coin["token_address"]
                    if coin.get("decimals") is not None:
                        coin_data["decimals"] = coin["decimals"]
                    suggestions.append(
                        {
                            "label": (
                                f"转 {coin['symbol']}"
                                if language == "zh"
                                else f"Transfer {coin['symbol']}"
                            ),
                            "message": (
                                f"转 {coin['symbol']}。"
                                if language == "zh"
                                else f"Transfer {coin['symbol']}."
                            ),
                            "type": "symbol",
                            "value": coin["symbol"],
                            **asset_logo_fields(
                                chain, coin["symbol"], coin.get("logo_url")
                            ),
                            "data": coin_data,
                        }
                    )
            else:
                # The amount card in the design offers 25/50/75/100% of the
                # balance; the App converts the percentage using the balance it
                # already shows. Carry the asset identity so the App can pin the
                # exact token on the card's asset-and-balance line.
                asset_data = dict(base_data)
                asset = _transfer_wallet_asset(context, chain, symbol)
                token_address = (
                    draft.get("transfer_token_address")
                    or draft.get("token_address")
                    or (asset.get("address") if asset else None)
                )
                decimals = draft.get("transfer_decimals")
                if decimals is None and asset is not None:
                    decimals = asset.get("decimals")
                if token_address and decimals is not None:
                    asset_data["token_address"] = token_address
                    asset_data["decimals"] = decimals
                for percent in ("25%", "50%", "75%", "100%"):
                    suggestions.append(
                        {
                            "label": f"用余额的 {percent}",
                            "message": f"用余额的 {percent} 转这笔。",
                            "type": "amount",
                            "value": percent,
                            # The amount card's 资产与余额 line shows the icon.
                            **asset_logo_fields(
                                chain,
                                symbol,
                                (asset or {}).get("logo_url") if asset else None,
                            ),
                            "data": asset_data,
                        }
                    )

        return _sanitize_response_suggestions(
            suggestions, intent="transfer", facts=facts
        )[: 3 if "transfer_recipient" in missing else 4]

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
                assets = [_balance_row_with_chain_logo(_dump(item)) for item in balances]
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
        adapter = execution_adapter(canonical_chain(chain))
        if adapter is None or not hasattr(adapter, "get_native_balance"):
            return {
                "ok": False,
                "error": "钱包数据服务暂不可用。",
                "code": "OKX_CAPABILITY_UNAVAILABLE",
            }
        try:
            balances = await _adapter_balance_rows(adapter, [address])
        except Exception as exc:
            return {"ok": False, "error": str(exc), "code": "WALLET_QUERY_FAILED"}
        assets = [_balance_row_with_chain_logo(_dump(item)) for item in balances]
        requested_symbol = canonical_symbol(str(symbol or ""))
        assets = _filter_balance_assets(assets, requested_symbol or None)
        native = next(
            (item for item in assets if not (item.get("asset") or {}).get("address")), None
        )
        return {
            "ok": True,
            "wallet": {
                "address": address,
                "chain": str(chain).upper(),
                "native_balance": native,
                "token_balances": [item for item in assets if item is not native],
                "source": "chain",
            },
            "asset_scope": ({"symbol": requested_symbol} if requested_symbol else None),
            "asset_count": len(assets),
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
            "dropped_or_replaced": (
                # Only EVM sequences transactions by nonce.
                "交易可能已被丢弃或替换，请检查钱包 nonce。"
                if canonical_chain(str(chain)) not in {"BTC", "TRON", "SOLANA"}
                else "交易可能已失效（未被打包或被网络丢弃），请重新发起。"
            ),
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
                    return {
                        "ok": True,
                        "transaction": {
                            **local,
                            **asset_logo_fields(local.get("chain")),
                        },
                    }
                # An ERC20/TRC20 transfer carries its real value in the token
                # layer; the native value is 0. Expose the moved asset up top so
                # the App status card can render amount + symbol directly, and
                # keep the full detail for the token_transfers[] row.
                token_transfers = list(getattr(detail, "token_transfers", None) or [])
                primary = token_transfers[0] if token_transfers else None
                if primary is not None and primary.amount not in (None, ""):
                    amount = primary.amount
                    symbol = primary.symbol or getattr(detail, "symbol", None)
                    logo_url = primary.logo_url or getattr(detail, "logo_url", None)
                else:
                    amount = getattr(detail, "value", None)
                    symbol = getattr(detail, "symbol", None)
                    logo_url = getattr(detail, "logo_url", None)
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
                        # Top-level "why" / ETA for the design's detail page.
                        "failure_reason": getattr(detail, "failure_reason", None),
                        "eta": getattr(detail, "eta", None),
                        # The moved amount + symbol (token layer first, then the
                        # native coin) so the App card never shows "-" or "0".
                        "amount": amount,
                        "symbol": symbol,
                        "detail": _dump(detail),
                        # The detail card's header shows the chain badge and the
                        # moved token's icon.
                        **asset_logo_fields(
                            str(chain).upper(),
                            symbol,
                            logo_url,
                        ),
                    },
                }
            except Exception as exc:
                if local is not None:
                    return {
                        "ok": True,
                        "transaction": {
                            **local,
                            **asset_logo_fields(local.get("chain")),
                        },
                    }
                return {"ok": False, "code": "TRANSACTION_STATUS_FAILED", "error": str(exc)}
        if local is not None:
            return {
                "ok": True,
                "transaction": {**local, **asset_logo_fields(local.get("chain"))},
            }
        return {
            "ok": False,
            "code": "OKX_CAPABILITY_UNAVAILABLE",
            "error": "交易查询服务暂不可用。",
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
                    # The gas row shows the native coin, whose icon is the chain
                    # logo.
                    **asset_logo_fields(str(chain).upper(), native.asset.symbol),
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
        serialized_assets = [
            {
                **_dump(asset),
                # Discovery rows are shown as a token list, so each one carries
                # the chain badge next to its icon.
                "chain_logo_url": chain_logo_url(asset.chain),
            }
            for asset in merged.values()
        ]
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
        if intent == "transaction_status":
            # The App passes the chain/hash as transaction_query; the planner only
            # sees the question text, so plan this call deterministically.
            query = _mapping(state.get("transaction_query"))
            tx_hash = str(
                query.get("tx_hash")
                or query.get("transaction_hash")
                or query.get("hash")
                or ""
            ).strip()
            if not tx_hash:
                return {
                    "response": {
                        "kind": "clarification",
                        "message": "请提供交易哈希，或从交易详情页进入。",
                        "missing_fields": ["transaction_hash"],
                    },
                    "read_tool_call": None,
                    "read_only_complete": True,
                    "read_only_pending": False,
                }
            arguments = {
                "chain": str(query.get("chain") or query.get("transaction_chain") or "").upper(),
                "tx_hash": tx_hash,
            }
            return {
                "read_tool_call": {
                    "name": "transaction_status",
                    "arguments": {key: value for key, value in arguments.items() if value},
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
            return {"ok": True, "prices": [_price_with_logos(item) for item in prices]}

        async def wallet_balance(args: dict[str, Any]) -> dict[str, Any]:
            """Read wallet balances."""
            if runtime.wallet_provider is not None and hasattr(
                runtime.wallet_provider, "get_token_balances"
            ):
                observation = await query_portfolio(
                    runtime.wallet_provider, state.get("wallet_context") or {}, args,
                    price_provider=runtime.price_provider,
                    timeout=max(0.1, min(
                        runtime.provider_timeout_seconds, runtime.react_tool_timeout_seconds - 3
                    )),
                )
                return {**observation, "wallet": observation["portfolio"]["wallet"]}
            context = state.get("wallet_context") or {}
            chain = args.get("chain") or context.get("chain")
            address = args.get("address") or context.get("address")
            if not chain or not address:
                return {
                    "ok": False,
                    "code": "WALLET_CONTEXT_REQUIRED",
                    "error": "请先连接钱包并明确查询网络。",
                }
            adapter = execution_adapter(str(chain))
            if adapter is None or not hasattr(adapter, "get_native_balance"):
                return {
                    "ok": False,
                    "code": "OKX_CAPABILITY_UNAVAILABLE",
                    "error": "该网络暂不支持余额查询。",
                }
            addresses = [str(address)]
            if _is_utxo_adapter(adapter):
                pooled = _utxo_source_addresses(context, str(chain), str(address))
                if pooled:
                    addresses = pooled
            try:
                assets = [_dump(item) for item in await _adapter_balance_rows(adapter, addresses)]
            except Exception as exc:
                return {"ok": False, "code": "WALLET_QUERY_FAILED", "error": str(exc)}
            requested_symbol = canonical_symbol(str(args.get("symbol") or ""))
            assets = _filter_balance_assets(assets, requested_symbol or None)
            native = next(
                (item for item in assets if not (item.get("asset") or {}).get("address")), None
            )
            return {
                "ok": True,
                "wallet": {
                    "address": str(address),
                    "chain": str(chain).upper(),
                    "native_balance": native,
                    "token_balances": [item for item in assets if item is not native],
                    "source": "chain",
                },
                "asset_scope": ({"symbol": requested_symbol} if requested_symbol else None),
                "asset_count": len(assets),
            }

        async def transaction_status(args: dict[str, Any]) -> dict[str, Any]:
            """Read a transaction status."""
            context = state.get("wallet_context") or {}
            # The App opens the assistant from a transaction detail screen with
            # only the question text; the chain/hash arrive as transaction_query.
            query = state.get("transaction_query") or {}
            return await transaction_status_tool(
                str(args.get("chain") or query.get("chain") or context.get("chain") or ""),
                str(
                    args.get("tx_hash")
                    or query.get("tx_hash")
                    or query.get("transaction_hash")
                    or query.get("hash")
                    or ""
                ),
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
                return {"ok": True, "details": [_price_with_logos(item) for item in details]}
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
                # A history page has no asset field of its own, so the resolved
                # identity travels next to it for the card's header icon.
                return {
                    "ok": True,
                    "history": _dump(page),
                    "asset": _asset_with_logo(asset.model_dump(mode="json")),
                    "chain_logo_url": chain_logo_url(asset.chain),
                }
            except Exception:
                return {"ok": False, "code": "PRICE_HISTORY_FAILED", "error": "历史价格暂不可用。"}

        async def portfolio_snapshot(args: dict[str, Any]) -> dict[str, Any]:
            return await query_portfolio(
                runtime.wallet_provider, state.get("wallet_context") or {}, args,
                price_provider=runtime.price_provider,
                timeout=max(0.1, min(
                    runtime.provider_timeout_seconds, runtime.react_tool_timeout_seconds - 3
                )),
            )

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
        summary = _swap_confirmation_summary(
            selected,
            slippage_bps=task_slippage,
            recipient_address=_swap_recipient_address(state, task),
        )
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
                "message": "请确认是否继续这笔兑换。",
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
        return {"quote_candidates": [_quote_with_chain_logos(payload["quote"])]}

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
            task_recreated = False
            if (
                not active_task
                or active_task.get("kind") != task_kind
                or active_task.get("status") in {"completed", "cancelled"}
            ):
                active_task = new_active_task(task_kind)
                task_recreated = True
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
            # Persist the App-pinned swap channel on the active task. The App
            # sends suggestion_data.provider once (the coin-selection turn) and
            # later amount/quote turns may omit it; keeping the pin on the task
            # makes the whole swap flow quote through the chosen provider. A
            # brand-new task that carries no provider must not inherit a pin
            # from a previous, unrelated swap.
            if task_kind == "swap":
                pinned = _suggestion_pinned_provider(state, runtime.providers)
                if pinned is not None:
                    if active_task.get("pinned_provider") != pinned:
                        active_task = {**active_task, "pinned_provider": pinned}
                        task_update["active_task"] = active_task
                elif task_recreated:
                    active_task = {**active_task, "pinned_provider": None}
                    task_update["active_task"] = active_task
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
                    # The previous transfer was already broadcast (or its status
                    # context is being cleared), so its unsigned prepared
                    # transaction and preflight are stale and must not be
                    # projected as a confirmation card for this new transfer.
                    pending_transaction=None,
                    preflight=None,
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
            # The App returns the user-picked BTC 出金地址 through
            # suggestion_data; pin it deterministically so the swap is quoted
            # from that exact address instead of relying on model extraction.
            request_meta = state.get("request") or {}
            suggestion = (
                request_meta.get("metadata")
                if isinstance(request_meta, Mapping)
                else None
            )
            if isinstance(suggestion, Mapping):
                suggestion = suggestion.get("suggestion_data")
            if (
                isinstance(suggestion, Mapping)
                and str(suggestion.get("intent") or "").strip().lower() == "swap"
                and str(suggestion.get("sender_address") or "").strip()
                and is_valid_address("BTC", str(suggestion["sender_address"]))
            ):
                draft["sender_address"] = str(suggestion["sender_address"]).strip()
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
            # A BTC-source swap must name one 出金 address before quoting: the
            # provider-facing fromAddress/userAddr is a single address (Lif:
            # “出金的地址只能从中间选一个”), and the App shows each BTC address
            # separately. When no explicit sender was chosen yet and the wallet
            # declared several BTC addresses, ask the App to select one instead
            # of silently picking the first account.
            btc_source_addresses = _swap_requires_btc_source_selection(
                draft, state.get("wallet_context")
            )
            if btc_source_addresses:
                active_task = _task_progress(
                    active_task,
                    slots=draft,
                    status="collecting",
                    stage="selecting_btc_address",
                    missing_fields=[],
                )
                address_options = await _btc_source_address_options(
                    btc_source_addresses
                )
                return {
                    "intent": "clarification",
                    "route": "clarification",
                    "swap_draft": draft,
                    **task_update,
                    "active_task": active_task,
                    "missing_fields": [],
                    **_task_update(
                        state.get("conversation_state"),
                        goal="swap",
                        stage="selecting_btc_address",
                        slots=draft,
                        missing_fields=[],
                    ),
                    "max_poll_attempts": runtime.max_poll_attempts,
                    "response": {
                        "kind": "clarification",
                        "message": "BTC 兑换需要确认出金地址，请在下面选择一个地址。",
                        "missing_fields": [],
                        "client_action": {
                            "type": "select_btc_address",
                            "reason": "BTC 兑换需要指定出金地址",
                            "slots": {
                                "chain": "BTC",
                                "symbol": "BTC",
                                "addresses": address_options,
                            },
                        },
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
        # The App can pin a single channel via suggestion_data.provider (Lif:
        # 测试 Bridgers 完整流程需要能指定渠道). Restrict the fan-out to that
        # provider so only it is quoted and can be selected/prepared.
        available_providers = list(runtime.providers)
        pinned_provider = _pinned_swap_provider(state, runtime.providers)
        if pinned_provider is not None:
            available_providers = [pinned_provider]
        if state.get("intent") == "swap_quote" and not available_providers:
            return {
                "swap_request": request_payload,
                "available_providers": [],
                "selected_quote": selected,
                "response": None,
                "errors": [
                    _error(
                        "PROVIDER_UNAVAILABLE",
                        "没有可用的兑换 Provider，或指定的渠道不可用。",
                        details={"capability": "quote"},
                    )
                ],
            }
        return {
            "swap_request": request_payload,
            "available_providers": available_providers,
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
        # Fee/rate/ETA display enrichment always runs (independent of the price
        # provider). reference_rate prefers the market price ratio and falls
        # back to the quoted output/input; total_payment is the source asset
        # the user pays including provider + network fees; eta is a documented
        # per-provider estimate unless the provider discloses a real one.
        enriched_display: list[dict[str, Any]] = []
        for item in quotes:
            q = dict(item)
            input_amount = Decimal(str(q.get("input_amount") or 0))
            output = Decimal(str(q.get("expected_output") or 0))
            network_fee = Decimal(str(q.get("network_fee") or 0))
            provider_fee = Decimal(str(q.get("provider_fee") or 0))
            reference_rate: Decimal | None = None
            snapshots = q.get("price_snapshots") or []
            source = q.get("source_asset") or {}
            destination = q.get("destination_asset") or {}
            src_price = _snapshot_price(snapshots, source)
            dst_price = _snapshot_price(snapshots, destination)
            if src_price is not None and dst_price:
                reference_rate = src_price / dst_price
            elif input_amount:
                reference_rate = output / input_amount
            if reference_rate is not None:
                q["reference_rate"] = str(reference_rate)
            q["total_payment"] = str(input_amount + network_fee + provider_fee)
            q["eta"] = q.get("eta") or _provider_eta_estimate(q.get("provider"))
            enriched_display.append(q)
        if enriched_display:
            quotes = enriched_display
        # Select only after enrichment so confirmation/session projection keep
        # USD values and price snapshots. Once the swap is complete the design
        # defaults to one channel (App-side report): when the user has no prior
        # selection, pick the best quote deterministically instead of making the
        # App render a channel picker. An explicit prior selection still wins.
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
            selected = _default_quote(quotes, state.get("swap_request"))
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
        stage = (
            "selecting_quote"
            if len(quotes) > 1 and selected is None
            else "ready_for_prepare"
        )
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
            builder = "build_native_transfer" if req.token is None else "build_erc20_transfer"
            if adapter is None or not hasattr(adapter, builder):
                return {
                    "response": {
                        "kind": "error",
                        "errors": [
                            _error(
                                "CHAIN_CAPABILITY_UNAVAILABLE",
                                f"{req.chain} 暂不支持构造转账交易。",
                                details={
                                    "chain": req.chain,
                                    "capability": builder,
                                    "kind": "token" if req.token else "native",
                                },
                            )
                        ],
                    }
                }
            if req.token is None:
                native_kwargs: dict[str, Any] = {}
                if _is_utxo_adapter(adapter):
                    sources = _utxo_source_addresses(
                        state.get("wallet_context"), req.chain, req.sender
                    )
                    if len(sources) > 1:
                        # Pool every address the App declared for the chain so a
                        # transfer is not capped at one address's UTXO balance.
                        native_kwargs["from_addresses"] = sources
                tx = await _resolve(
                    adapter.build_native_transfer(
                        from_address=req.sender,
                        to_address=req.recipient,
                        amount_raw=req.amount_raw,
                        **native_kwargs,
                    )
                )
            else:
                tx = await _resolve(
                    adapter.build_erc20_transfer(
                        token=req.token,
                        from_address=req.sender,
                        to_address=req.recipient,
                        amount_raw=req.amount_raw,
                    )
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
            # The unsigned-transaction card shows what moves next to the signing
            # payload, so keep the asset identity (with its icon) outside the
            # transaction the wallet signs.
            transfer_asset = (
                _asset_with_logo(req.token.model_dump(mode="json"))
                if req.token is not None
                else _asset_with_logo(
                    {
                        "chain": canonical_chain(req.chain),
                        "symbol": native_symbol(req.chain) or req.chain,
                        "decimals": native_decimals(req.chain),
                        "address": None,
                    }
                )
            )
            return {
                "preflight": preflight,
                "pending_transaction": tx.model_dump(mode="json"),
                "response": {
                    "kind": "transfer_prepare",
                    "pending_transaction": tx.model_dump(mode="json"),
                    "preflight": preflight,
                    "asset": transfer_asset,
                    "chain_logo_url": chain_logo_url(req.chain),
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
                    **_swap_status_logo_context(state),
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
            prepared = await _resolve_swap_transaction(
                prepared,
                quote=quote,
                adapter=adapter,
                sender=str(request.get("sender_address") or ""),
                chain=source_chain,
                wallet_context=state.get("wallet_context"),
            )
            if isinstance(prepared, UnsignedTransaction) and adapter is not None:
                preflight = await _transaction_preflight(
                    adapter=adapter,
                    chain=source_chain,
                    sender=str(request.get("sender_address") or ""),
                    recipient=_swap_preflight_recipient(prepared, request, source_chain),
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
                    asset=_asset_with_logo(source_asset),
                    chain_logo_url=chain_logo_url(source_chain),
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
            approval = await _resolve(
                adapter.build_erc20_approve(
                    token=requirement.token,
                    owner=requirement.owner,
                    spender=requirement.spender,
                    amount_raw=requirement.required_amount_raw,
                )
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
                chain_logo_url=chain_logo_url(requirement.token.chain),
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
        prepared = await _resolve_swap_transaction(
            prepared,
            quote=quote,
            adapter=adapter,
            sender=str(request.get("sender_address") or requirement.owner),
            chain=source_chain,
            wallet_context=state.get("wallet_context"),
        )
        if isinstance(prepared, UnsignedTransaction) and adapter is not None:
            preflight = await _transaction_preflight(
                adapter=adapter,
                chain=source_chain,
                sender=str(request.get("sender_address") or requirement.owner),
                recipient=_swap_preflight_recipient(
                    prepared,
                    {
                        **request,
                        "recipient_address": request.get("recipient_address")
                        or requirement.owner,
                    },
                    source_chain,
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
                    asset=_asset_with_logo(source_asset),
                    chain_logo_url=chain_logo_url(source_chain),
                    **({"preflight": preflight} if preflight is not None else {}),
                ),
            }

    async def wallet_query(state: dict[str, Any]) -> dict[str, Any]:
        request = state.get("request", {})
        context = state.get("wallet_context") or {}
        chain = str(request.get("chain", ""))
        address = str(request.get("address", ""))
        use_okx = runtime.wallet_provider is not None and hasattr(
            runtime.wallet_provider, "get_token_balances"
        )
        if use_okx:
            index = okx_chain_index(chain)
            if index is None:
                return {
                    "errors": [_error("OKX_CHAIN_UNSUPPORTED", f"Chain {chain} is unavailable.")]
                }
            try:
                balances = await runtime.wallet_provider.get_token_balances(address, [index])
            except Exception as exc:
                return {"errors": [_error("WALLET_QUERY_FAILED", str(exc))]}
        else:
            adapter = execution_adapter(chain)
            if adapter is None or not hasattr(adapter, "get_native_balance"):
                return {
                    "errors": [
                        _error(
                            "OKX_CAPABILITY_UNAVAILABLE",
                            "No wallet data provider is available for this chain.",
                        )
                    ]
                }
            addresses = [address]
            if _is_utxo_adapter(adapter):
                pooled = _utxo_source_addresses(context, chain, address)
                if pooled:
                    addresses = pooled
            try:
                balances = await _adapter_balance_rows(adapter, addresses)
            except Exception as exc:
                return {"errors": [_error("WALLET_QUERY_FAILED", str(exc))]}
        native = next((item for item in balances if item.asset.address is None), None)
        snapshot = {
            "address": address,
            "chain": chain,
            "native_balance": _dump(native) if native else None,
            "token_balances": [
                _dump(item) for item in balances if item.asset.address is not None
            ],
            "source": "okx" if use_okx else "chain",
        }
        return {
            "wallet_context": snapshot,
            "response": {"kind": "wallet_query", "wallet": snapshot},
        }

    async def portfolio_query(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("portfolio_request") or {}
        context = state.get("wallet_context") or {}
        chain = str(raw.get("chain") or context.get("chain") or "").upper()
        address = str(raw.get("address") or context.get("address") or "")
        adapter = execution_adapter(chain) if chain else None
        use_okx = runtime.wallet_provider is not None and hasattr(
            runtime.wallet_provider, "get_token_balances"
        )
        if not address:
            return {
                "response": {
                    "kind": "clarification",
                    "message": "请先连接钱包，或提供钱包地址。",
                    "missing_fields": ["wallet_address"],
                }
            }
        source = "okx"
        balances: list[Any] = []
        if use_okx:
            index = okx_chain_index(chain)
            if index is not None:
                try:
                    balances = await runtime.wallet_provider.get_token_balances(address, [index])
                except Exception as exc:
                    if adapter is None:
                        return {
                            "response": {
                                "kind": "error",
                                "errors": [
                                    _error("PORTFOLIO_QUERY_FAILED", str(exc), retryable=True)
                                ],
                            }
                        }
                    source = "chain"
            elif adapter is None:
                return {
                    "response": {
                        "kind": "error",
                        "errors": [
                            _error("OKX_CHAIN_UNSUPPORTED", f"Chain {chain} is unavailable.")
                        ],
                    }
                }
            else:
                source = "chain"
        else:
            source = "chain"
        if source == "chain":
            if adapter is None or not hasattr(adapter, "get_native_balance"):
                return {
                    "response": {
                        "kind": "error",
                        "errors": [
                            _error(
                                "OKX_CAPABILITY_UNAVAILABLE",
                                "No wallet data provider is available for this chain.",
                            )
                        ],
                    }
                }
            addresses = [address]
            if _is_utxo_adapter(adapter):
                pooled = _utxo_source_addresses(context, chain, address)
                if pooled:
                    addresses = pooled
            try:
                balances = await _adapter_balance_rows(adapter, addresses)
            except Exception as exc:
                return {
                    "response": {
                        "kind": "error",
                        "errors": [_error("PORTFOLIO_QUERY_FAILED", str(exc), retryable=True)],
                    }
                }
        try:
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
                "source": source,
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
                **asset_logo_fields(chain, native.asset.symbol),
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
                    {
                        "kind": "price_query",
                        "prices": [_price_with_logos(item) for item in prices],
                    },
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
                failure_reason = getattr(detail, "failure_reason", None)
                snapshot = {
                    "chain": chain,
                    "tx_hash": tx_hash,
                    "status": status_value,
                    "message": {
                        "pending": "交易已提交，正在等待链上确认。",
                        "confirmed": "交易已确认。",
                        "failed": (
                            f"交易执行失败：{failure_reason}"
                            if failure_reason
                            else "交易执行失败或已回滚。"
                        ),
                        "unknown": "暂时无法确定交易状态。",
                    }.get(status_value, "暂时无法确定交易状态。"),
                    "failure_reason": failure_reason,
                    "reason_unavailable": status_value == "failed" and not failure_reason,
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
                        "Transaction explorer unavailable.",
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
        quote = _quote(selected)
        try:
            prepared = await provider.prepare(quote)
            preflight = None
            request = state.get("swap_request") or {}
            source_asset = request.get("source_asset") or selected.get("source_asset") or {}
            source_chain = str(source_asset.get("chain") or selected.get("chain") or "")
            adapter = execution_adapter(source_chain)
            prepared = await _resolve_swap_transaction(
                prepared,
                quote=quote,
                adapter=adapter,
                sender=str(request.get("sender_address") or ""),
                chain=source_chain,
                wallet_context=state.get("wallet_context"),
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
                    recipient=_swap_preflight_recipient(
                        prepared_transaction, request, source_chain
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
                    # The unsigned-transaction card shows what is swapped out.
                    "asset": _asset_with_logo(quote.source_asset.model_dump(mode="json")),
                    "chain_logo_url": chain_logo_url(quote.source_asset.chain),
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
        provider_visible = {
            "broadcast_seen",
            "broadcast_pending",
            "source_confirmed",
            "confirmed",
        }
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
                **_swap_status_logo_context(state),
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
            # Pass the persisted quote so the provider can rebuild its full
            # registration payload when the in-memory quote cache is cold
            # (process restart / another provider instance), instead of sending
            # a minimal fallback that Bridgers rejects with resCode 101.
            order = await provider.register_broadcast(
                str(provider_reference), tx_hash, quote=selected or None
            )
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
        provider_visible = {
            "broadcast_seen",
            "broadcast_pending",
            "source_confirmed",
            "confirmed",
            None,
        }
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
                **_swap_status_logo_context(state),
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
                    **_swap_status_logo_context(state),
                }
                return {
                    "response": await naturalize_read_response(state, response_payload)
                }
            response_payload = {
                "kind": "swap_status",
                "status": "unknown",
                "message": status_messages["unknown"],
                **_swap_status_logo_context(state),
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
            # The source receipt alone is never swap completion. broadcast_status
            # stays source_confirmed while the order is processing, and is only
            # promoted to confirmed once the provider order itself completes, so
            # clients keying success off either field see it at the same moment.
            effective_broadcast_status = (
                "confirmed" if provider_status == "completed" else broadcast_status
            )
            broadcast_message = {
                "broadcast_seen": "源链已收到交易广播",
                "broadcast_pending": "源链交易已广播，正在等待链上确认",
                "source_confirmed": "源链交易已确认",
                "confirmed": "源链交易已确认",
            }.get(effective_broadcast_status)
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
                "broadcast_status": effective_broadcast_status,
                "confirmation_status": effective_broadcast_status,
                "tx_hash": state.get("broadcast_tx_hash"),
                # The design's 预计耗时 / 解释失败原因 surface on the detail
                # page: expose them at the top level so the App does not dig
                # into the nested order status.
                "failure_reason": dumped.get("failure_reason"),
                "eta": dumped.get("eta"),
                "message": message,
                **_swap_status_logo_context(state),
            }
            return {
                "status_snapshot": dumped,
                "broadcast_status": effective_broadcast_status,
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
        tx_hash: str = "",
        chain: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        # The App can open this from a transaction or swap order detail page
        # with the chain/hash carried in the entry context; fall back to it so
        # the model does not need to restate (or guess) the chain.
        query = state.get("swap_query") or state.get("transaction_query") or {}
        resolved_hash = str(tx_hash or query.get("tx_hash") or query.get("hash") or "")
        resolved_chain = str(
            chain
            or query.get("chain")
            or state.get("wallet_context", {}).get("chain")
            or ""
        )
        return await _react_read_observation(
            "transaction_status",
            {"tx_hash": resolved_hash, "chain": resolved_chain},
            state,
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
        recipient_address: str | None = None,
        slippage_bps: int | None = None,
        provider_name: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        """Resolve human asset names and request one or all provider quotes.

        Pass ``recipient_address`` whenever the user names where the swapped
        asset should land (an exchange address, a different account, or the
        target-chain address of a cross-chain swap).
        """
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
            "recipient_address": recipient_address,
            "slippage_bps": slippage_bps,
        }
        # The App returns the user-picked BTC 出金地址 through suggestion_data;
        # carry it so the quote is sent from that exact address instead of the
        # first declared account (Lif: “出金的地址只能从中间选一个”).
        request_meta = state.get("request") or {}
        suggestion = (
            request_meta.get("metadata") if isinstance(request_meta, Mapping) else None
        )
        if isinstance(suggestion, Mapping):
            suggestion = suggestion.get("suggestion_data")
        if (
            isinstance(suggestion, Mapping)
            and str(suggestion.get("intent") or "").strip().lower() == "swap"
            and str(suggestion.get("sender_address") or "").strip()
            and is_valid_address("BTC", str(suggestion["sender_address"]))
        ):
            values["sender_address"] = str(suggestion["sender_address"]).strip()
        # The App can pin a single channel via suggestion_data.provider; quote
        # through only that provider so it can be selected and prepared (Lif:
        # 测试 Bridgers 完整授权流程需要能指定渠道). The App selection wins over
        # whatever the model guessed for provider_name.
        pinned_provider = _pinned_swap_provider(state, runtime.providers)
        if pinned_provider is not None:
            provider_name = pinned_provider
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
        # The user's own words name the source/destination chain; a model guess
        # that contradicts them must not win. In a cross-chain swap such as
        # “用eth的usdt换trx” the model sometimes mirrors the target chain back
        # onto the source (TRON USDT as source) and reports ASSET_NOT_FOUND.
        # Lif's real session 20e62b6a hit exactly that transient 报价不存在.
        message_hints = swap_direction_hints(
            str((current.get("request") or {}).get("message") or "")
        )
        for chain_key in ("source_chain", "destination_chain"):
            hinted = message_hints.get(chain_key)
            if hinted and draft.get(chain_key):
                if canonical_chain(str(draft[chain_key])) != hinted:
                    draft[chain_key] = hinted
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
        # A BTC-source swap must name one 出金 address before quoting. Ask the
        # App to pick one when the wallet declared several BTC addresses and
        # none was explicitly chosen, instead of silently quoting from the
        # first account.
        btc_source_addresses = _swap_requires_btc_source_selection(resolved, context)
        if btc_source_addresses:
            address_options = await _btc_source_address_options(btc_source_addresses)
            return {
                "ok": False,
                "code": "BTC_SOURCE_ADDRESS_REQUIRED",
                "error": "BTC 兑换需要确认出金地址。",
                "missing_fields": ["sender_address"],
                "client_action": {
                    "type": "select_btc_address",
                    "reason": "BTC 兑换需要指定出金地址",
                    "slots": {
                        "chain": "BTC",
                        "symbol": "BTC",
                        "addresses": address_options,
                    },
                },
            }
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

    async def react_receive_address(
        chain: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        """The App's own receive addresses; nothing is read from a chain."""
        payload = _receive_address_payload(state.get("wallet_context"))
        canonical = canonical_chain(str(chain or ""))
        if canonical and canonical != "ALL":
            groups = [
                item
                for item in payload.get("groups") or []
                if canonical in item.get("chains", [])
            ]
            if groups:
                payload = {
                    **payload,
                    "groups": groups,
                    "address": groups[0]["address"],
                }
        return payload

    async def _explorer_history(
        address: str, chain: str | None, limit: int, timeout: float
    ) -> tuple[list[Any] | None, str | None]:
        explorer = getattr(runtime, "explorer_provider", None)
        if explorer is None or not hasattr(explorer, "get_transaction_history"):
            return None, "unavailable"
        try:
            page = await asyncio.wait_for(
                explorer.get_transaction_history(address, chain, limit=limit), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None, "timeout"
        except Exception as exc:
            return None, (str(exc) or "failed")
        return list(getattr(page, "transactions", None) or []), None

    async def _adapter_history(chain: str, address: str, limit: int) -> list[Any] | None:
        adapter = execution_adapter(chain)
        if adapter is None or not hasattr(adapter, "get_transaction_history"):
            return None
        try:
            records = await adapter.get_transaction_history(address, limit=limit)
        except Exception:
            return None
        return list(records or [])

    async def react_request_client_action(
        action: str,
        reason: str | None = None,
        chain: str | None = None,
        symbol: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        """Ask the App to open one of its own surfaces (contacts, scanner, keypad)."""
        name = str(action or "").strip()
        if name not in _CLIENT_ACTION_TYPES:
            return {
                "ok": False,
                "code": "CLIENT_ACTION_UNSUPPORTED",
                "error": f"App 不支持的动作：{name or '未提供'}。",
                "allowed_actions": list(_CLIENT_ACTION_TYPES),
            }
        slots = {
            key: value
            for key, value in {
                "chain": canonical_chain(str(chain or "")) or None,
                "symbol": str(symbol or "").strip().upper() or None,
            }.items()
            if value
        }
        return {
            "ok": True,
            "client_action": {
                "type": name,
                "reason": str(reason or "").strip() or None,
                "slots": slots,
            },
        }

    async def react_transfer_quick_replies(
        chain: str | None = None,
        symbol: str | None = None,
        amount: str | None = None,
        recipient: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        """Standard quick replies for the transfer card's parameter collection."""
        missing = [
            field
            for field, value in (
                ("transfer_chain", chain),
                ("transfer_recipient", recipient),
                ("transfer_amount", amount),
            )
            if not value
        ]
        if not missing:
            return {"ok": True, "missing_fields": [], "suggestions": []}
        draft = {
            key: value
            for key, value in (
                ("chain", chain),
                ("symbol", symbol),
                ("amount", amount),
                ("recipient", recipient),
            )
            if value
        }
        suggestions = await transfer_suggestions({**dict(state), "transfer_draft": draft}, missing)
        return {
            "ok": True,
            "missing_fields": missing,
            "suggestions": suggestions,
        }

    async def react_swap_quick_replies(
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        """Standard quick replies for the swap card's pair selection."""
        language = _message_language(
            str((state.get("request") or {}).get("message") or "")
        )
        suggestions = await _react_swap_pair_suggestions(
            wallet_provider=runtime.wallet_provider,
            wallet_context=state.get("wallet_context") or {},
            tool_payloads=_react_turn_tool_payloads(state),
            read_facts=state.get("read_facts"),
            language=language,
        )
        return {"ok": True, "missing_fields": [], "suggestions": suggestions}

    async def react_transaction_history(
        chain: str | None = None,
        direction: str | None = None,
        limit: int | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        """Recent transactions across the App's declared accounts."""
        context = state.get("wallet_context") or {}
        requested = canonical_chain(str(chain or ""))
        requested = "" if requested == "ALL" else requested
        targets = _history_targets(context, chain)
        if not targets:
            return {
                "ok": False,
                "code": "HISTORY_ADDRESS_UNAVAILABLE",
                "error": "App 还没有提供任何公共地址，无法查询交易记录。",
            }
        cap = max(1, min(int(limit or 10), 50))
        chains_by_address: dict[str, list[str]] = {}
        for item_chain, address in targets:
            chains = chains_by_address.setdefault(address, [])
            if item_chain not in chains:
                chains.append(item_chain)

        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        failures: list[str] = []
        # OKX rejects a query without a chain, so always scope one chain per
        # call. Some chains are very slow, so every call is capped and a slow
        # chain is reported instead of sinking the whole card.
        jobs: list[tuple[str, str]] = []
        for address, address_chains in chains_by_address.items():
            for item_chain in address_chains:
                if requested and item_chain != requested:
                    continue
                jobs.append((item_chain, address))
        skipped = jobs[12:]
        jobs = jobs[:12]
        budget = max(2.0, float(getattr(runtime, "react_tool_timeout_seconds", 20)) - 6)
        per_call = max(2.0, min(8.0, budget))
        # OKX rate-limits a burst of history queries, which surfaces as timeouts.
        gate = asyncio.Semaphore(3)

        async def fetch(chain: str, address: str) -> tuple[str, list[Any], str | None]:
            async with gate:
                records, error = await _explorer_history(address, chain, cap, per_call)
                if error is None:
                    return chain, list(records or []), None
                fallback = await _adapter_history(chain, address, cap)
            if fallback:
                return chain, list(fallback), None
            # No explorer answer and nothing from the fallback: report it rather
            # than letting the chain look like "no transactions". An EVM adapter
            # that swallows an unsupported RPC call is exactly this case.
            return chain, [], f"{chain}:{error}"

        tasks = [asyncio.create_task(fetch(*job)) for job in jobs]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=budget)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for (item_chain, address), task in zip(jobs, tasks):
            if task.cancelled():
                failures.append(f"{item_chain}:timeout")
                continue
            try:
                _, records, failure = task.result()
            except Exception:
                failures.append(f"{item_chain}:failed")
                continue
            if failure:
                failures.append(failure)
            for record in records:
                item = _transaction_history_item(record, address)
                if requested and canonical_chain(str(item.get("chain") or "")) != requested:
                    continue
                key = (str(item.get("chain") or ""), str(item.get("tx_hash") or ""))
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
        failures.extend(f"{item_chain}:skipped" for item_chain, _ in skipped)

        payload = _transaction_history_payload(
            items, direction=direction, chain=requested or chain, limit=cap
        )
        if failures:
            payload["partial_errors"] = sorted(set(failures))
            if payload["items"]:
                payload["message"] += "（部分地址本轮未返回。）"
        return payload

    _SWAP_STATUS_MESSAGES = {
        "pending": "兑换订单已提交，Provider 仍在处理中。",
        "processing": "兑换服务仍在处理中，目标链资产尚未到账。",
        "completed": "兑换已完成，可以到目标链查看到账资产。",
        "failed": "兑换没有成功完成，请检查订单详情或稍后重试。",
        "refunded": "兑换已退款，请检查原资产是否已退回。",
        "timed_out": "Provider 暂时没有返回最终结果，建议稍后重新查询订单状态。",
        "kyc_required": "订单需要补充 KYC 后才能继续处理。",
        "unknown": "暂时拿不到兑换订单的最新状态，建议稍后重新查询。",
    }

    def _swap_status_observation(status: Any, state: dict[str, Any]) -> dict[str, Any]:
        dumped = _dump(status)
        provider_status = str(dumped.get("status") or "unknown")
        return {
            "ok": True,
            "kind": "swap_status",
            "provider": dumped.get("provider"),
            "provider_order_id": dumped.get("provider_order_id"),
            "status": provider_status,
            "tx_hash": dumped.get("tx_hash"),
            "failure_reason": dumped.get("failure_reason"),
            "eta": dumped.get("eta"),
            "refund_tx_hash": dumped.get("refund_tx_hash"),
            "message": _SWAP_STATUS_MESSAGES.get(
                provider_status, "兑换订单状态已更新，建议稍后重新查询。"
            ),
        }

    async def _swap_status_by_order_id(
        order_id: str, state: dict[str, Any]
    ) -> dict[str, Any] | None:
        # Prefer an order already registered in this conversation's state (the
        # swap lifecycle records provider_orders once the hash is broadcast).
        orders = state.get("provider_orders") or {}
        for provider_name, raw_order in orders.items():
            if not isinstance(raw_order, Mapping):
                continue
            if str(raw_order.get("provider_order_id") or "") != order_id:
                continue
            provider = (runtime.providers or {}).get(str(provider_name))
            if provider is None:
                continue
            try:
                status = await provider.get_status(ProviderOrder.model_validate(raw_order))
            except Exception:
                continue
            return _swap_status_observation(status, state)
        # The App can open the assistant straight from an order detail page with
        # only swap_query.order_id and no loaded session; reconstruct the order
        # and probe the registered providers. Bridgers' status endpoint needs
        # only the order id, while OmniBridge additionally needs its
        # equipment_no, so a provider that does not own the order errors out and
        # is skipped (bounded to the few configured providers).
        for provider_name, provider in (runtime.providers or {}).items():
            try:
                status = await provider.get_status(
                    ProviderOrder(
                        provider=str(provider_name),
                        provider_order_id=order_id,
                        provider_reference=order_id,
                    )
                )
            except Exception:
                continue
            return _swap_status_observation(status, state)
        return None

    async def react_query_swap_status(
        order_id: str | None = None,
        tx_hash: str | None = None,
        chain: str | None = None,
        *,
        state: Annotated[dict[str, Any], InjectedState],
    ) -> dict[str, Any]:
        # The App opens this from a swap order detail page carrying
        # swap_query.order_id; prefer the provider order status so the answer
        # reflects the actual order (a chain transaction lookup for the source
        # hash alone is not the order and fails).
        query = state.get("swap_query") or state.get("transaction_query") or {}
        resolved_order_id = str(
            order_id
            or query.get("provider_order_id")
            or query.get("order_id")
            or query.get("session_id")
            or ""
        ).strip()
        if resolved_order_id:
            observation = await _swap_status_by_order_id(resolved_order_id, state)
            if observation is not None:
                return observation
        # Fall back to the transaction-status context when no order resolves.
        return await transaction_status_tool(
            str(chain or query.get("chain") or state.get("wallet_context", {}).get("chain") or ""),
            str(tx_hash or query.get("tx_hash") or query.get("hash") or ""),
            state.get("broadcast_status"),
        )

    react_tools = [
        StructuredTool.from_function(
            coroutine=react_wallet_balance,
            name="wallet_balance",
            description=(
                "查询 App 全部支持网络和账户的余额。不传 chain 查询全部；"
                "用户仅指定币种时只传 symbol；明确指定网络时才传 chain。"
                "签名钱包当前链不代表查询范围。返回逐链覆盖状态，必须披露缺失地址或失败。"
            ),
        ),
        StructuredTool.from_function(
            coroutine=react_portfolio_snapshot,
            name="portfolio_snapshot",
            description=(
                "查询 App 全部支持网络和公共地址的资产。不传 chain 查询全部；"
                "仅指定币种只传 symbol；明确指定网络才传 chain。"
                "返回完整资产明细、逐链 network_status 和明细总值；"
                "缺失地址或失败必须告知用户。BTC仅覆盖已提供地址。"
            ),
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
            coroutine=react_receive_address,
            name="receive_address",
            description=(
                "获取用户自己的收款地址（收款码）。地址来自 App 提供的公共地址："
                "所有 EVM 网络共用一个地址，BTC/TRON 等各自独立。"
                "用户要“收款地址/收款码/我的地址/别人怎么给我转账”时调用；"
                "用户问“你还支持哪些链”时用返回里的 supported_chains 回答。"
                "注意：这里返回的是用户自己的地址，不是向用户索要对方的转账地址。"
            ),
        ),
        StructuredTool.from_function(
            coroutine=react_get_swap_quote,
            name="get_swap_quote",
            description="根据来源/目标资产和数量获取兑换报价；自动发现合约和 decimals。",
        ),
        StructuredTool.from_function(
            coroutine=react_swap_quick_replies,
            name="swap_quick_replies",
            description=(
                "生成兑换意图的标准快捷选项：根据用户余额推荐交易对，以及"
                "“其他币对”“跨链换”入口。当用户表达兑换意图但还没有指定"
                "卖出的币种和买入的币种时先调用它，再把返回的选项连同你的"
                "提问一起给用户；已经明确币对时不要调用，直接用 get_swap_quote。"
            ),
        ),
        StructuredTool.from_function(
            coroutine=react_transaction_history,
            name="transaction_history",
            description=(
                "查询用户最近的交易记录（流水）。默认覆盖 App 声明的全部地址与网络；"
                "可用 chain 只看某条链，用 direction 取值 send/receive/swap 实现"
                "“只看支出/只看入账/只看兑换”。返回最近记录、笔数汇总和未确认提示。"
                "用户问“我最近有哪些交易/交易记录/流水/花了多少钱”时调用。"
            ),
        ),
        StructuredTool.from_function(
            coroutine=react_transfer_quick_replies,
            name="transfer_quick_replies",
            description=(
                "生成转账参数收集用的标准快捷选项（收款地址／转给联系人／先看看余额、"
                "网络、Token、金额）。当你需要向用户补充转账信息时先调用它，"
                "再把返回的选项连同你的提问一起给用户；不要自己编造选项。"
                "用户提供收款地址后，必须把该地址作为 recipient 再次调用本工具，"
                "以取得下一步的网络选择卡（type:\"chain\"）选项并展示；"
                "不要用文字罗列可选网络。"
            ),
        ),
        StructuredTool.from_function(
            coroutine=react_request_client_action,
            name="request_client_action",
            description=(
                "请求 App 弹出它自己的界面并把结果回填给这一轮对话。"
                "action 取值：open_contacts（通讯录选联系人）、scan_qr（扫收款码）、"
                "open_amount_keypad（数字键盘输入金额）、"
                "address_input（弹出地址输入框让用户粘贴收款地址）、"
                "select_btc_address（BTC 兑换时让用户在多个 BTC 地址中选择出金地址）。"
                "用户说“从联系人选/转给朋友”“扫一扫”“输入金额”，"
                "或需要用户提供收款地址时调用它，"
                "不要用文字向用户索要通讯录、二维码或键盘里的数据，也不要自己编造联系人。"
            ),
        ),
        StructuredTool.from_function(
            coroutine=react_prepare_transfer,
            name="prepare_transfer",
            description="准备未签名转账交易；自动发现 Token 合约和 decimals。",
        ),
        StructuredTool.from_function(
            coroutine=react_query_swap_status,
            name="query_swap_status",
            description=(
                "查询兑换订单的当前状态（按 order_id 查 Provider 订单；"
                "没有订单时可用 tx_hash+chain 查链上交易状态）。"
                "订单详情页的场景要优先传/使用订单上下文里的 order_id，"
                "不要只用来源链交易哈希去猜。"
            ),
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
                "余额工具不传 chain 默认覆盖 App 支持网络；"
                "钱包当前网络仅用于签名，不能据此缩小查询范围。"
                "只问 BNB 等币种时传 symbol 不要自动限制网络；明确只查某链时才传 chain。"
                "依据 network_status 说明查询范围和缺失/失败，"
                "不能把未查询当零余额，不能把部分余额称全部。"
                "保留极小但非零的美元余额，显示 <0.01；总值使用工具中同一份明细的 total_usd_value。"
                "BTC 余额仅覆盖 App 提供的地址；未提供 BTC 地址时请提示 App 补充公共地址。"
                "用户索要自己的收款地址或收款码时调用 receive_address 工具，不要反问用户要地址；"
                "所有 EVM 网络共用一个收款地址，BTC/TRON 等各自独立，并说明支持的链。"
                "用户问最近交易、交易记录或流水时调用 transaction_history 工具，"
                "按方向筛选时传 direction，不要凭空推测交易。"
                "交易记录里 raw_only 为 true 的条目金额是代币最小单位，"
                "不要换算或估算，原样说明即可。"
                "缺少转账参数时先调用 transfer_quick_replies 取得标准快捷选项，再提问；"
                "逐步收集（先地址、再网络、再金额），网络未明确时给链选择卡，"
                "不要默认使用钱包当前网络。"
                "用户提供收款地址后，立即用该地址调用 transfer_quick_replies"
                "（recipient=该地址）取得网络选择卡并展示给用户；"
                "禁止用文字罗列可选网络。"
                "用户表达兑换意图但还没指定币对时，先调用 swap_quick_replies"
                "根据余额推荐交易对，再把返回的选项连同你的提问一起给用户；"
                "已经明确币对时直接用 get_swap_quote，不要重复推荐。"
                "需要通讯录、扫码、金额键盘、地址输入或 BTC 出金地址选择时"
                "调用 request_client_action 请求 App 弹出，"
                "自己不要收集这些界面里的数据。"
                "回复保持纯文本短句：禁止使用 Markdown 图片（如 ![ETH](url)）和表格，"
                "禁止用 <suggestions>、JSON 或任何标签把选项写进文案；"
                "币种与链的图标只通过结构化字段下发，由 App 渲染，不要写进文案；"
                "选项会由系统自动附带在结构化 suggestions 字段里，"
                "文案不要重复罗列网络、币种或金额选项。"
                "短追问要结合历史理解；工具失败时说明真实原因并提出最小澄清；不要编造事实或描述内部推理。"
            )
        )
        # The App can open the assistant from a transaction detail screen with a
        # default question ("explain the failure"). Without this the model only
        # sees the question text and has no hash to check.
        tx_context = state.get("transaction_query") or request.get("transaction_query")
        if isinstance(tx_context, Mapping):
            tx_chain = str(
                tx_context.get("chain") or tx_context.get("transaction_chain") or ""
            ).upper()
            tx_hash = str(
                tx_context.get("tx_hash")
                or tx_context.get("transaction_hash")
                or tx_context.get("hash")
                or ""
            ).strip()
            if tx_hash:
                system = SystemMessage(
                    content=str(system.content)
                    + f"当前交易上下文：链 {tx_chain or '未知'}，交易哈希 {tx_hash}。"
                    "用户多半是从这笔交易的详情页进来的，要解释它的状态、失败原因或为什么还在处理中；"
                    "先用 transaction_status 工具查证再回答，"
                    "工具没给失败原因时就说明来源没有提供，不要编造。"
                )

        # The App can also open the assistant from a swap order detail screen
        # ("为什么还在处理中？") and pass its order context, mirroring the
        # transaction entry above.
        swap_context = state.get("swap_query") or request.get("swap_query")
        if isinstance(swap_context, Mapping):
            order_id = str(
                swap_context.get("provider_order_id")
                or swap_context.get("order_id")
                or swap_context.get("session_id")
                or ""
            ).strip()
            tx_hash = str(swap_context.get("tx_hash") or "").strip()
            swap_chain = str(
                swap_context.get("chain") or swap_context.get("swap_chain") or ""
            ).strip().upper()
            if order_id or tx_hash:
                system = SystemMessage(
                    content=str(system.content)
                    + (
                        f"当前兑换订单上下文：订单 {order_id}。"
                        if order_id
                        else "当前兑换订单上下文。"
                    )
                    + (f"订单链 {swap_chain}。" if swap_chain else "")
                    + ("广播哈希 " + tx_hash + "。" if tx_hash else "")
                    + "用户多半是从兑换详情页进来的，要解释它的状态、失败原因或为什么还在处理中；"
                    "先用 query_swap_status / transaction_status 查证再回答，"
                    "来源没给失败原因时就说明没有提供，不要编造。"
                    "这是订单详情分析，不要调用 transaction_history 拉取最近交易流水，"
                    "也不要重新开始一笔新的兑换。"
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
        old_system = next((item for item in existing if isinstance(item, SystemMessage)), None)
        additions: list[Any] = (
            [SystemMessage(content=system.content, id=old_system.id)] if old_system else [system]
        )
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
        # Ephemeral context reflects the current App snapshot, not an earlier
        # account or signing network persisted in the conversation.
        messages = [
            item
            for item in messages
            if not (
                isinstance(item, SystemMessage)
                and str(item.content).startswith("Current wallet App context (public data):")
            )
        ]
        messages.insert(
            1,
            SystemMessage(
                content="Current wallet App context (public data): "
                + json.dumps(state.get("wallet_context") or {}, ensure_ascii=False)
            ),
        )
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
        tools = react_tools
        if state.get("swap_query"):
            # Swap order detail analysis: the recent-transaction history card
            # must not project over the order's status explanation. Keep the
            # tool (the conversation may return to general questions) but hide
            # it from this order-scoped turn.
            tools = [
                tool for tool in react_tools if getattr(tool, "name", "") != "transaction_history"
            ]
        parameters = inspect.signature(invoke_react).parameters.values()
        accepts_model_id = any(
            parameter.name == "model_id"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if accepts_model_id:
            result = await invoke_react(messages, tools, model_id=request_model_id)
        else:
            result = await invoke_react(messages, tools)
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
        # The App renders message as a short plain-text sentence. Deterministic
        # safety net behind the prompt constraint: drop Markdown images/tables
        # the model may still emit so the client never has to render them.
        content = _plain_text_message(content)
        if not content:
            content = "我暂时无法生成有效回复，请换一种方式描述你的需求。"
        current_user_index = max(
            (
                index
                for index, message in enumerate(messages)
                if isinstance(message, HumanMessage)
            ),
            default=-1,
        )
        tool_payloads = _react_turn_tool_payloads(state)
        react_results = [
            {"tool": name, "result": payload} for name, payload in tool_payloads
        ]
        # A tool may hand back the standard quick replies (for example when the
        # transfer card still needs a recipient); surface them on the response
        # so the App can render the chip row next to the question.
        quick_replies = next(
            (
                list(payload.get("suggestions") or [])
                for _, payload in reversed(tool_payloads)
                if isinstance(payload.get("suggestions"), list) and payload["suggestions"]
            ),
            None,
        )
        response: dict[str, Any] = {
            "kind": "react",
            "message": content,
            "react_results": react_results,
            "react_tool_timings": list(state.get("react_tool_timings") or []),
            "graph_role": "conversation",
            "route_mode": state.get("route_mode") or "react",
            "route_reason": state.get("route_reason") or "model_answer",
        }
        # A BTC swap must name its 出金 address before the amount step, so a
        # select_btc_address surface outranks the amount keypad when the model
        # requested both in one turn.
        client_action = next(
            (
                dict(payload["client_action"])
                for _, payload in tool_payloads
                if isinstance(payload.get("client_action"), Mapping)
                and str(payload["client_action"].get("type") or "") == "select_btc_address"
            ),
            None,
        )
        if client_action is None:
            client_action = next(
                (
                    dict(payload["client_action"])
                    for _, payload in reversed(tool_payloads)
                    if isinstance(payload.get("client_action"), Mapping)
                ),
                None,
            )
        current_message = str(
            (state.get("request") or {}).get("message") or state.get("message") or ""
        ).strip()
        # A swap intent covers both the explicit swap tokens and a message that
        # names a full pair ("USDT 兑 ETH", "用 USDT 换 ETH", "swap USDT to ETH").
        # Those pair messages carry no "兑换/换成" token, and treating them as a
        # transfer would drop the swap amount keypad (App-side report: keypad
        # only appeared in react_results, never top-level).
        swap_intent = (
            (
                any(
                    token in current_message.lower()
                    for token in ("swap", "exchange", "兑换", "换成")
                )
                or _react_swap_pair_from_message(current_message) is not None
            )
            and not _looks_like_transfer_message(current_message)
        )
        swap_keypad_slots = None
        if client_action and str(client_action.get("type") or "") == "open_amount_keypad":
            swap_keypad_slots = _react_swap_keypad_slots(
                dict(client_action.get("slots") or {}), state, swap_intent
            )
            if swap_keypad_slots is not None:
                client_action = {**client_action, "slots": swap_keypad_slots}
        # The model can pin the coin via request_client_action (keypad
        # slots.symbol) and also call transfer_quick_replies whose draft has no
        # symbol yet, surfacing an amount keypad and a coin chip in the same
        # round. Reconcile them so the App renders one consistent amount step.
        # The keypad's symbol only counts when the user actually chose it; the
        # model defaulting to the chain's native coin must not skip the
        # coin-selection step, or the user gets locked to that coin.
        if (
            swap_keypad_slots is None
            and client_action
            and str(client_action.get("type") or "") == "open_amount_keypad"
        ):
            keypad_slots = dict(client_action.get("slots") or {})
            keypad_symbol = str(keypad_slots.get("symbol") or "").strip()
            if keypad_symbol and _react_transfer_user_symbol(state):
                if "decimals" not in keypad_slots:
                    decimals = next(
                        (
                            (item.get("data") or {}).get("decimals")
                            for item in (quick_replies or [])
                            if (item.get("data") or {}).get("symbol") == keypad_symbol
                            and (item.get("data") or {}).get("decimals") is not None
                        ),
                        None,
                    )
                    if decimals is None:
                        asset = _transfer_wallet_asset(
                            state.get("wallet_context") or {},
                            str(keypad_slots.get("chain") or ""),
                            keypad_symbol,
                        )
                        if asset is not None:
                            decimals = asset.get("decimals")
                    if decimals is None:
                        decimals = _NATIVE_ASSET_DECIMALS.get(
                            canonical_chain(str(keypad_slots.get("chain") or ""))
                        )
                    if decimals is not None:
                        keypad_slots["decimals"] = decimals
                        client_action = {**client_action, "slots": keypad_slots}
                if quick_replies:
                    quick_replies = [
                        item
                        for item in quick_replies
                        if str(item.get("type") or "") not in {"symbol", "token"}
                    ]
            else:
                # The coin was not user-chosen (unknown, or the model defaulted
                # the native coin): a keypad cannot anchor a raw amount, so drop
                # it and let the coin card drive the step.
                client_action = None
        # The model can anchor on an unfinished transfer task and call
        # transfer_quick_replies even when the user asked to swap (e.g. "我要兑换"
        # right after a BTC transfer). Rendering the transfer receive-address card
        # over a swap clarification contradicts the Figma swap card, so for a clear
        # swap request that produced no swap quote this turn, drop the transfer
        # suggestions instead of projecting the wrong card.
        swap_quote_missing = swap_intent and _best_react_quote_result(tool_payloads) is None
        if swap_quote_missing:
            # The model may already have called swap_quick_replies to recommend
            # pairs; keep those. Only transfer-collection suggestions would
            # project the wrong receive-address card, so drop just those.
            swap_tool_suggestions = next(
                (
                    list(payload.get("suggestions") or [])
                    for name, payload in reversed(tool_payloads)
                    if name != "transfer_quick_replies"
                    and isinstance(payload.get("suggestions"), list)
                    and payload["suggestions"]
                ),
                None,
            )
            quick_replies = swap_tool_suggestions if swap_tool_suggestions else None
        transfer_collection = any(
            name == "transfer_quick_replies" for name, _ in tool_payloads
        )
        if quick_replies:
            response["suggestions"] = quick_replies
        elif client_action is None and current_user_index >= 0 and not swap_intent:
            # The model answered a transfer-collection step with prose instead
            # of calling transfer_quick_replies. The App renders cards from
            # suggestions, not prose, so deterministically attach the card for
            # the next missing slot (chain → coin → amount) to keep the flow
            # intact regardless of model behavior.
            transfer_card = _react_transfer_collection(state)
            if transfer_card is not None:
                transfer_collection = True
                missing, draft = transfer_card
                card_suggestions = await transfer_suggestions(
                    {**dict(state), "transfer_draft": draft}, missing
                )
                if card_suggestions:
                    response["suggestions"] = card_suggestions
        # A pair can come from the current message ("把 ETH 换成 USDT") or, for a
        # bare "兑换" confirmation, from an earlier turn that already named it.
        # Reusing the established pair keeps the swap on track instead of
        # bouncing back to the recommended-pair card (App-side report: after
        # "USDT 换 ETH" a bare "兑换" re-showed the recommendation card).
        current_pair = _react_swap_pair_from_message(current_message)
        established_pair = (
            _react_established_swap_pair(state)
            if _react_swap_continuation(current_message)
            else None
        )
        active_pair = current_pair or established_pair
        # Figma swap-intent card ("好的，想换什么？"): when the user expresses
        # an open-ended swap request and the model produced only prose (no quote,
        # no client surface, no suggestions), deterministically recommend pairs
        # from the user's balance so the App never renders an empty bubble.
        if (
            swap_quote_missing
            and client_action is None
            and not response.get("suggestions")
            and active_pair is None
        ):
            swap_rows = _swap_balance_rows(
                state.get("wallet_context") or {}, tool_payloads, state.get("read_facts")
            )
            if _swap_recommendation_desired(current_message, swap_rows):
                swap_suggestions = await _react_swap_pair_suggestions(
                    wallet_provider=runtime.wallet_provider,
                    wallet_context=state.get("wallet_context") or {},
                    tool_payloads=tool_payloads,
                    read_facts=state.get("read_facts"),
                    language=_message_language(current_message),
                )
                if swap_suggestions:
                    response["suggestions"] = swap_suggestions
        # Swap source coin held on several networks (Figma 33715:134430): when
        # the user names an explicit pair ("用 USDT 换 ETH") but the source coin
        # lives on multiple networks and the model only asked in prose, offer the
        # structured chain-selection card instead of making the user type a
        # network name. Each chip pins chain + symbol + token metadata so the
        # next turn can go straight to the quote.
        if (
            client_action is None
            and _best_react_quote_result(tool_payloads) is None
            and not response.get("suggestions")
        ):
            if (
                active_pair is not None
                and _react_swap_pinned_chain(state) is None
                and not _looks_like_transfer_message(current_message)
            ):
                source_symbol, destination_symbol = active_pair
                chain_rows = _swap_source_chain_rows(
                    state.get("wallet_context") or {},
                    tool_payloads,
                    state.get("read_facts"),
                    source_symbol,
                )
                if len(chain_rows) >= 2:
                    ordered_chains = _order_supported_chains(
                        list(chain_rows), (state.get("wallet_context") or {}).get("chain")
                    )
                    language = _message_language(current_message)
                    chain_suggestions: list[dict[str, Any]] = []
                    for chain in ordered_chains:
                        descriptor = chain_rows[chain]
                        symbol = descriptor["symbol"]
                        data: dict[str, Any] = {
                            "intent": "swap",
                            "chain": chain,
                            "symbol": symbol,
                        }
                        if descriptor.get("token_address"):
                            data["token_address"] = descriptor["token_address"]
                        if descriptor.get("decimals") is not None:
                            data["decimals"] = descriptor["decimals"]
                        chain_suggestions.append(
                            {
                                "label": (
                                    f"使用 {chain} 网络的 {symbol}"
                                    if language != "en"
                                    else f"Use {symbol} on {chain}"
                                ),
                                "message": (
                                    f"用 {chain} 网络的 {symbol} 换 {destination_symbol}"
                                    if language != "en"
                                    else f"Swap {symbol} on {chain} to {destination_symbol}"
                                ),
                                "type": "chain",
                                "value": chain,
                                "chain_logo_url": chain_logo_url(chain),
                                "data": data,
                            }
                        )
                    if chain_suggestions:
                        response["suggestions"] = chain_suggestions
        if client_action:
            response["client_action"] = client_action
        update: dict[str, Any] = {"response": response, "react_active": False}
        # Persist an App-pinned swap channel across ReAct turns. The App sends
        # suggestion_data.provider on the coin-selection turn and may omit it on
        # later amount/quote turns; storing it on the active task keeps the whole
        # swap flow quoting/preparing through the chosen provider. A completed or
        # cancelled task (or a non-swap task) never keeps a stale pin, so a new
        # unpinned swap does not inherit the previous one.
        pinned_provider = _suggestion_pinned_provider(state, runtime.providers)
        if pinned_provider is not None:
            active_task = state.get("active_task")
            if (
                not isinstance(active_task, Mapping)
                or str(active_task.get("kind") or "") != "swap"
                or active_task.get("status") in {"completed", "cancelled"}
            ):
                active_task = new_active_task("swap")
            if active_task.get("pinned_provider") != pinned_provider:
                active_task = {**active_task, "pinned_provider": pinned_provider}
                update["active_task"] = active_task
        portfolio_result = next((payload.get("portfolio") for _, payload in reversed(tool_payloads)
                                 if isinstance(payload.get("portfolio"), Mapping)), None)
        # A transfer amount step drives a client surface (amount keypad /
        # address input / contacts / scanner) via client_action. Elevating a
        # wallet_balance observation to a top-level portfolio card in the same
        # round would make the App render an asset-overview card next to the
        # keypad in one bubble, so only a balance question without a client
        # action projects a portfolio card.
        if portfolio_result is not None and client_action is None:
            response["portfolio"] = dict(portfolio_result)
        quote_result = _best_react_quote_result(tool_payloads)
        if quote_result is not None:
            # The design defaults to one channel and renders the confirmation
            # card directly once coin/amount/chain are complete (App-side
            # report), instead of making the user compare providers. Keep the
            # candidates projectable for audit, but also pick the deterministic
            # best quote so the API can continue straight into confirmation.
            update["quote_candidates"] = [
                {"__clear__": True},
                *[dict(item) for item in quote_result["quotes"] if isinstance(item, Mapping)],
            ]
            if isinstance(quote_result.get("request"), Mapping):
                update["swap_request"] = dict(quote_result["request"])
            default_quote = _default_quote(
                [item for item in quote_result["quotes"] if isinstance(item, Mapping)],
                quote_result.get("request"),
            )
            if default_quote is not None:
                update["selected_quote"] = default_quote
                # A fresh quote selection invalidates the previous
                # confirmation cycle (for example a cancelled/rejected swap).
                # If the stale ``confirmation_state`` were left in the
                # checkpoint, the API's automatic swap_prepare continuation
                # would see it and route_after_intent would short-circuit to
                # "response", so the client never receives
                # confirmation_required for the new quote. Only an unexpired
                # pending confirmation for the SAME quote is kept, so an
                # incidental re-quote does not drop an in-flight card.
                previous_confirmation = state.get("confirmation_state") or {}
                active_task = hydrate_active_task(state)
                task_slippage = (
                    ((active_task or {}).get("slots") or {}).get("slippage_bps")
                )
                new_quote_hash = confirmation_payload_hash(
                    _swap_confirmation_summary(
                        default_quote,
                        slippage_bps=task_slippage,
                        recipient_address=_swap_recipient_address(state, active_task),
                    )
                )
                still_valid = (
                    isinstance(previous_confirmation, Mapping)
                    and previous_confirmation.get("status") == "requested"
                    and not _confirmation_expired(previous_confirmation)
                    and previous_confirmation.get("payload_hash") == new_quote_hash
                )
                if not still_valid:
                    update["confirmation_state"] = None
                    update["user_confirmation"] = None
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
        elif state.get("pending_transaction") is not None:
            # A prepared unsigned transaction is only actionable while it is
            # still awaiting signature. Once it has been broadcast, or this
            # react turn is collecting parameters for a NEW transfer (quick
            # replies / client surface / transfer card, with no fresh prepare),
            # the old unsigned transaction no longer matches what the user is
            # doing. Drop it here so the App stops rendering the stale
            # confirmation card next to the current turn's card.
            transfer_collection = (
                transfer_collection
                or client_action is not None
            )
            if state.get("broadcast_tx_hash") or transfer_collection:
                update["pending_transaction"] = None
                update["preflight"] = None
        return update

    return locals()
