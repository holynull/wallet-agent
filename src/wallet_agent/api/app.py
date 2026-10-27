"""REST and SSE endpoints for embedding the agent in a wallet app."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, model_validator

from wallet_agent.chains._common import receipt_success
from wallet_agent.domain.chains import (
    asset_logo_fields,
    asset_logo_url,
    chain_logo_url,
    native_symbol,
    token_logo_url,
)
from wallet_agent.domain.errors import ChainCapabilityUnavailable
from wallet_agent.domain.models import (
    AgentError,
    Asset,
    DepositOrder,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    UnsignedTransaction,
)
from wallet_agent.domain.normalization import canonical_symbol, detect_message_language
from wallet_agent.okx import (
    OKX_CHAIN_INDEX_BY_NAME,
    OkxExplorerError,
    OkxWalletError,
)
from wallet_agent.persistence import (
    TERMINAL_RUN_STATUSES,
    InMemoryLockManager,
    InMemoryRunStore,
    InMemorySessionStore,
    LockManager,
    RunEvent,
    RunStore,
    SessionRevisionConflict,
    SessionStore,
    SwapSessionRecord,
)
from wallet_agent.prices.okx import PriceProviderError

from .auth import TokenVerifier
from .dependencies import authenticated_user, is_admin_token
from .inspection import build_run_summary, sanitize_debug_value

_AGENT_TEST_INTENTS = {
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

_BROADCAST_RPC_ATTEMPTS = 3
_BROADCAST_RPC_DELAY_SECONDS = 0.2
#: Two ways a chain can report on a wallet-returned hash. EVM exposes a
#: transaction receipt; Bitcoin and the account-model chains expose only a
#: status lookup, so the probe result is mapped onto the same broadcast states.
_BROADCAST_STATUS_BY_PROBE = {
    "confirmed": "confirmed",
    "pending": "broadcast_pending",
    "failed": "failed",
    "dropped": "dropped_or_replaced",
}
BroadcastStatus = Literal[
    "broadcast_seen",
    "broadcast_pending",
    "not_propagated",
    # Source-chain transaction is confirmed, but the cross-chain swap order is
    # still being processed (target-chain assets not yet delivered). Clients
    # must not treat this as swap completion; only order_status / a terminal
    # swap state may signal "success".
    "source_confirmed",
    "confirmed",
    "failed",
    "dropped_or_replaced",
    "unknown",
]
_BROADCAST_PROVIDER_VISIBLE: frozenset[BroadcastStatus] = frozenset(
    {"broadcast_seen", "broadcast_pending", "source_confirmed", "confirmed"}
)
#: Swap order states that terminate the order lifecycle. Once reached there is
#: nothing left to poll and the swap is done (success, failure or refund).
_SWAP_ORDER_TERMINAL_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "refunded", "timed_out", "kyc_required", "cancelled"}
)


class WalletAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chain: str = Field(min_length=1, max_length=32)
    address: str = Field(min_length=1, max_length=128)
    # Public signing material for UTXO chains, supplied by the App. A PSBT signer
    # needs the pubkey, its derivation path and the master fingerprint; none of
    # these can spend on their own.
    pubkey: str | None = Field(default=None, max_length=130)
    derivation_path: str | None = Field(default=None, max_length=128)
    master_fingerprint: str | None = Field(default=None, max_length=16)


class ConversationUpdateRequest(BaseModel):
    """Rename or pin a conversation. Absent fields are left untouched."""

    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    message: str
    address: str | None = None
    chain: str | None = None
    supported_networks: list[str] | None = Field(default=None, max_length=50)
    wallet_accounts: list[WalletAccount] | None = Field(default=None, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_id: str | None = None

    @model_validator(mode="after")
    def reject_signing_material(self) -> "TurnRequest":
        def visit(value: Any) -> bool:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    canonical = "".join(c for c in str(key).lower() if c.isalnum())
                    if canonical in {
                        "privatekey",
                        "seedphrase",
                        "mnemonic",
                        "signer",
                        "walletclient",
                        "secret",
                        "password",
                        "accesstoken",
                        "clientsecret",
                        "apikey",
                        "apibaseurl",
                        "baseurl",
                    } or any(fragment in canonical for fragment in ("privatekey", "seedphrase")):
                        return True
                    if visit(child):
                        return True
            elif isinstance(value, (list, tuple)):
                return any(visit(item) for item in value)
            return False

        if visit(self.metadata):
            raise ValueError("signing material and credentials are not accepted")
        return self


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    approved: bool


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None


class BroadcastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    chain: str
    tx_hash: str = Field(min_length=8)


class ApprovalBroadcastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str | None = None
    chain: str
    approve_tx_hash: str = Field(min_length=8)


class QuoteSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str | None = None
    provider_reference: str


class TransferPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    chain: str
    sender: str
    recipient: str
    amount: str
    amount_raw: str
    token: Asset | None = None


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _balance_logo_item(value: Any) -> dict[str, Any]:
    """Balance row with the chain logo next to its asset (whose own logo lives
    on ``asset.logo_url`` when the provider supplied one)."""
    dumped = _jsonable(value)
    if not isinstance(dumped, Mapping):
        return dumped
    asset = dumped.get("asset")
    chain = asset.get("chain") if isinstance(asset, Mapping) else None
    if chain:
        dumped["chain_logo_url"] = chain_logo_url(chain)
        logo = token_logo_url(
            str(chain), str(asset.get("symbol") or ""), fallback=asset.get("logo_url")
        )
        if not logo and asset.get("address"):
            logo = asset_logo_url(str(chain), str(asset["address"]))
        # Native coins have no provider logo, so fall back to the chain logo
        # rather than leaving the App to combine two fields itself.
        dumped["asset"] = {
            **asset,
            "logo_url": logo,
        }
    return dumped


def _history_logo_item(value: Any) -> dict[str, Any]:
    """Transaction row with the chain logo and a token logo (provider logo, else
    the native coin's chain logo)."""
    dumped = _jsonable(value)
    if not isinstance(dumped, Mapping):
        return dumped
    chain = dumped.get("chain")
    if chain:
        dumped["chain_logo_url"] = chain_logo_url(chain)
        dumped["logo_url"] = dumped.get("logo_url") or token_logo_url(
            chain, dumped.get("symbol")
        )
    return dumped


def _price_logo_item(value: Any) -> dict[str, Any]:
    """Price/market row with the asset's icon and the chain badge."""
    dumped = _jsonable(value)
    if not isinstance(dumped, Mapping):
        return dumped
    asset = dumped.get("asset")
    if not isinstance(asset, Mapping) or not asset.get("chain"):
        return dumped
    chain = str(asset["chain"])
    logo = token_logo_url(
        chain, str(asset.get("symbol") or ""), fallback=asset.get("logo_url")
    )
    if not logo and asset.get("address"):
        logo = asset_logo_url(chain, str(asset["address"]))
    return {
        **dumped,
        "asset": {
            **asset,
            "logo_url": logo,
        },
        "chain_logo_url": chain_logo_url(chain),
    }


def _asset_logo_item(value: Any) -> dict[str, Any]:
    """Asset metadata row (discovery/catalog) with its icons attached."""
    dumped = _jsonable(value)
    if not isinstance(dumped, Mapping) or not dumped.get("chain"):
        return dumped
    logo = token_logo_url(
        str(dumped["chain"]),
        str(dumped.get("symbol") or ""),
        fallback=dumped.get("logo_url"),
    )
    if not logo and dumped.get("address"):
        logo = asset_logo_url(str(dumped["chain"]), str(dumped["address"]))
    return {
        **dumped,
        "logo_url": logo,
        "chain_logo_url": chain_logo_url(str(dumped["chain"])),
    }


def _conversation_title(message: Any, *, limit: int = 40) -> str | None:
    """First user utterance, whitespace-collapsed, used as the history row title."""
    text = " ".join(str(message or "").split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _conversation_summary(session: Any) -> dict[str, Any]:
    """List-row projection shared by the list and update endpoints."""
    if session.quote is not None:
        fallback = (
            f"{session.quote.source_asset.symbol} → {session.quote.destination_asset.symbol}"
        )
    else:
        fallback = "Wallet Agent 对话"
    return {
        "conversation_id": session.thread_id,
        "session_id": session.session_id,
        "title": session.title or fallback,
        "pinned": bool(getattr(session, "pinned", False)),
        "status": session.status,
        "stage": session.stage,
        "updated_at": session.updated_at,
        "created_at": session.created_at,
        "summary": fallback,
    }


def _inspection_session_summary(session: Any) -> dict[str, Any]:
    """Safe session fields needed to join a run to its business lifecycle."""
    if session is None:
        return {}
    summary = _conversation_summary(session)
    summary.update(
        {
            "last_run_id": session.last_run_id,
            "selected_provider_reference": session.selected_provider_reference,
            "broadcast_tx_hash": session.broadcast_tx_hash,
            "broadcast_status": session.broadcast_status,
        }
    )
    return summary


def _conversation_cursor(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc


def _conversation_artifacts(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project one response into the card snapshot persisted beside its chat message."""
    raw_response = state.get("response") or {}
    if isinstance(raw_response, Mapping) and isinstance(raw_response.get("response"), Mapping):
        raw_response = raw_response["response"]
    if not isinstance(raw_response, Mapping) or not raw_response:
        return None
    response = _jsonable(raw_response)
    kind = response.get("kind")
    stage = response.get("stage") or (state.get("authorization_stage") if not kind else None)
    route_metadata = {
        key: _jsonable(state.get(key))
        for key in ("graph_role", "route_mode", "route_reason")
        if state.get(key) is not None
    }
    has_route_metadata = bool(route_metadata)
    if route_metadata and not response.get("route"):
        response["route"] = route_metadata
    tool_timings = state.get("react_tool_timings")
    has_tool_timings = isinstance(tool_timings, list) and bool(tool_timings)
    if has_tool_timings and not response.get("react_tool_timings"):
        response["react_tool_timings"] = _jsonable(tool_timings)
    artifacts: dict[str, Any] = {"response": response}
    model_trace = state.get("model_trace")
    if isinstance(model_trace, list) and model_trace:
        artifacts["model_trace"] = _jsonable(model_trace)

    # ReAct responses already contain the current turn's validated tool
    # observations. Preserve them as a generic artifact so the UI can restore
    # the exact tool trace without guessing a legacy business-card kind.
    react_results = response.get("react_results")
    if isinstance(react_results, list) and react_results:
        artifacts["react_results"] = _jsonable(react_results)

    if kind == "swap_quote":
        artifacts["quotes"] = response.get("quotes") or _jsonable(
            state.get("quote_candidates") or []
        )
    if kind == "confirmation_required":
        artifacts["confirmation"] = response.get("confirmation") or _jsonable(
            state.get("confirmation_state")
        )
    if kind == "approval_required" or stage == "approval_required":
        artifacts["approval"] = response.get("approval_transaction") or _jsonable(
            state.get("approval_transaction")
        )
    if kind in {"transfer_prepare", "swap_prepare"} or stage == "swap_ready":
        artifacts["preflight"] = response.get("preflight") or _jsonable(
            state.get("preflight")
        )
        artifacts["pending_transaction"] = (
            response.get("pending_transaction")
            or response.get("transaction")
            or _jsonable(state.get("pending_transaction"))
        )
        artifacts["pending_kind"] = "transfer" if kind == "transfer_prepare" else "swap"
    if kind == "clarification":
        candidates = response.get("token_candidates") or state.get("token_candidates") or []
        if candidates:
            artifacts["token_candidates"] = _jsonable(candidates)
    if kind == "wallet_query":
        artifacts["wallet"] = response.get("wallet") or response
    if kind == "portfolio_query":
        artifacts["portfolio"] = response.get("portfolio") or response
    if isinstance(response.get("portfolio"), Mapping):
        artifacts["portfolio"] = response["portfolio"]
    if kind == "price_query":
        artifacts["price"] = response
    if kind == "asset_value":
        artifacts["asset_value"] = response
    if kind == "gas_check":
        artifacts["gas"] = response
    if kind == "asset_discovery":
        artifacts["assets"] = response
    if kind in {"swap_status", "transaction_status"}:
        artifacts["status"] = response
    if kind == "error" and (response.get("preflight") or state.get("preflight")):
        artifacts["preflight"] = response.get("preflight") or _jsonable(state.get("preflight"))
    if state.get("broadcast_tx_hash") or response.get("tx_hash"):
        artifacts["broadcast_hash"] = state.get("broadcast_tx_hash") or response.get("tx_hash")

    read_only_fields = {
        "enabled": state.get("read_only_enabled") is True,
        "loop_count": int(state.get("read_loop_count") or 0),
        "tool_calls": _jsonable(state.get("read_tool_calls") or []),
        "facts": _jsonable(state.get("read_facts") or {}),
        "reference": _jsonable(state.get("read_reference")),
    }
    if (
        read_only_fields["enabled"]
        or read_only_fields["loop_count"]
        or read_only_fields["tool_calls"]
        or read_only_fields["facts"]
        or read_only_fields["reference"]
    ):
        artifacts["read_only"] = read_only_fields

    return artifacts if len(artifacts) > 1 or has_route_metadata or has_tool_timings else None


def _is_bitcoin_psbt_transaction(value: Any) -> bool:
    """Bitcoin transfers arrive as PSBTs rather than signable EVM calldata."""
    if not isinstance(value, Mapping):
        return False
    display = value.get("display")
    if isinstance(display, Mapping) and str(display.get("format", "")).lower() == "psbt":
        return True
    return str(value.get("chain", "")).upper() in {"BTC", "BITCOIN"}


def _assistant_response_content(response: Mapping[str, Any]) -> str:
    if response.get("message"):
        return str(response["message"])
    errors = response.get("errors") or []
    if errors and isinstance(errors[0], Mapping):
        return str(errors[0].get("message") or errors[0].get("code") or "请求处理失败。")
    kind = response.get("kind")
    stage = response.get("stage")
    if kind == "swap_quote":
        count = len(response.get("quotes") or [])
        message = f"我找到了 {count} 个兑换报价，请比较后选择一个 Provider。"
        failed = [
            str((item.get("details") or {}).get("provider") or "未知 Provider")
            for item in response.get("provider_errors") or []
            if isinstance(item, Mapping)
        ]
        if failed:
            message += f"另有 {', '.join(dict.fromkeys(failed))} 报价失败，已跳过。"
        return message
    pending = response.get("pending_transaction") or response.get("transaction")
    if _is_bitcoin_psbt_transaction(pending) and (
        kind in {"transfer_prepare", "swap_prepare"} or stage == "swap_ready"
    ):
        target = "转账" if kind == "transfer_prepare" else "兑换"
        return f"Bitcoin {target}交易已准备好，请把 PSBT 导入比特币钱包签名并广播。"
    messages = {
        "unsupported": "抱歉，我目前只能处理钱包余额、资产、价格、转账和兑换相关请求。",
        "confirmation_required": "请确认是否继续这笔兑换。",
        "approval_required": "这笔兑换需要先授权 Token 额度，请在钱包中确认 Approve。",
        "transfer_prepare": "转账交易已准备好，请在钱包中确认并广播。",
        "swap_prepare": "兑换交易已准备好，请在钱包中确认并广播。",
        "wallet_query": "钱包余额查询完成。",
        "portfolio_query": "资产组合查询完成。",
        "price_query": "价格查询完成。",
        "asset_value": "资产估值查询完成。",
        "gas_check": "Gas 检查完成。",
        "asset_discovery": "Token 资产查询完成。",
        "transaction_status": "交易状态查询完成。",
        "swap_status": "兑换状态查询完成。",
        "error": "请求处理失败。",
    }
    stage_messages = {
        "approval_required": "这笔兑换需要先授权 Token 额度，请在钱包中确认 Approve。",
        "swap_ready": "兑换交易已准备好，请在钱包中确认并广播。",
        "quote_selection_required": "请选择一个兑换报价后继续。",
    }
    return messages.get(str(kind)) or stage_messages.get(str(stage)) or "请求已处理完成。"


def _assistant_history_entry(
    state: Mapping[str, Any], *, turn_id: str | None = None
) -> dict[str, Any] | None:
    response = state.get("response") or {}
    if not isinstance(response, Mapping) or not response:
        return None
    entry: dict[str, Any] = {
        "role": "assistant",
        "content": _assistant_response_content(response),
    }
    if turn_id:
        # One run can persist several distinct responses (a quote, then the
        # confirmation it triggered), so the id needs a content fingerprint:
        # re-persisting the same response stays idempotent, a different one is
        # kept as its own transcript row.
        fingerprint = hashlib.sha1(
            json.dumps(
                {key: entry.get(key) for key in ("content", "artifacts", "suggestions")},
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:12]
        entry["id"] = f"assistant:{turn_id}:{fingerprint}"
    artifacts = _conversation_artifacts(state)
    if artifacts:
        entry["artifacts"] = artifacts
    suggestions = response.get("suggestions")
    if suggestions:
        entry["suggestions"] = _jsonable(suggestions)
    return entry


#: State the App never consumes but every SSE frame used to carry. The
#: LangChain message list alone (system prompt, token usage, raw tool payloads)
#: was ~30 KiB per turn and dominated the App's debug logs; ``request`` and
#: ``wallet_context`` echo back what the App just sent, and the rest is internal
#: bookkeeping. Measured on a two-turn conversation: 191.6 KiB -> see
#: tests/api/test_stream_payload.py for the assertion that keeps this small.
_STREAM_PRIVATE_KEYS = frozenset(
    {
        "messages",
        "request",
        "wallet_context",
        "dialogue",
        "facts",
        "operation_history",
        "supervisor_decision",
        "supervisor_output",
        "predicted_intent",
    }
)

#: ReAct loop node updates whose only App-relevant content is the LangChain
#: ``messages`` list (system prompt, per-message token usage, raw tool payloads)
#: published under the node key. The App renders none of it — it only logs these
#: frames — and once the internals are stripped the rest is bookkeeping
#: (react_steps, route fields, ...), so the whole frame is dropped instead of
#: shipping the message list Lif flagged in the turn logs.
_STREAM_REACT_NODES = frozenset({"react_entry", "react_agent", "react_tools"})

#: A history entry the App may render; card artifacts stay in the conversation
#: endpoint, and internal fields never leave the checkpoint.
_STREAM_HISTORY_KEYS = ("role", "content", "id", "suggestions")


def _stream_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the App-facing projection of graph state for an SSE frame."""
    public_state = _jsonable(state)
    for key in _STREAM_PRIVATE_KEYS:
        public_state.pop(key, None)
    history = public_state.get("conversation_history")
    if isinstance(history, list):
        public_state["conversation_history"] = [
            {key: item[key] for key in _STREAM_HISTORY_KEYS if key in item}
            for item in history
            if isinstance(item, Mapping)
        ]
    return public_state


def _model_trace_for_state(
    state: Mapping[str, Any], model_registry: Any = None
) -> list[dict[str, Any]]:
    trace = state.get("model_trace")
    if isinstance(trace, list):
        return [_jsonable(item) for item in trace if isinstance(item, Mapping)]
    if model_registry is not None and hasattr(model_registry, "role_model_ids"):
        return [
            {"role": role, "model_id": model_id}
            for role, model_id in model_registry.role_model_ids.items()
        ]
    return []


def _broadcast_status_messages(language: str) -> dict[str, str]:
    """Localized broadcast/swap-status card messages.

    ``language`` comes from the persisted session (``SwapSessionRecord.
    language``), resolved from the conversation at turn time, so the App's
    status cards never mix languages the way a hard-coded string could.
    """
    if language == "en":
        return {
            "confirmed": (
                "Source-chain transaction confirmed; the swap provider is "
                "processing and the target-chain assets are not delivered yet."
            ),
            "source_confirmed": (
                "Source-chain transaction confirmed; the swap provider is "
                "processing and the target-chain assets are not delivered yet."
            ),
            "broadcast_pending": (
                "Source-chain transaction broadcast; waiting for on-chain "
                "confirmation."
            ),
            "broadcast_seen": (
                "The source chain observed this transaction; the swap provider "
                "is processing and the target-chain assets are not delivered yet."
            ),
            "not_propagated": (
                "The source chain has not found this transaction yet; the hash "
                "was saved. Refresh the broadcast status later (on-chain data "
                "sync may lag)."
            ),
            "unknown": (
                "The source-chain broadcast status could not be confirmed; the "
                "hash was saved. Please retry later."
            ),
            "dropped_or_replaced": (
                "The source-account nonce advanced without this transaction "
                "being found; it may have been replaced or dropped. Check your "
                "wallet and broadcast again."
            ),
        }
    return {
        "confirmed": "源链交易已确认，兑换服务正在处理，目标链资产尚未确认到账。",
        "source_confirmed": "源链交易已确认，兑换服务正在处理，目标链资产尚未确认到账。",
        "broadcast_pending": "源链交易已广播，兑换服务正在处理，目标链资产尚未确认到账。",
        "broadcast_seen": "源链已发现这笔交易，兑换服务正在处理，目标链资产尚未确认到账。",
        "not_propagated": (
            "源链暂时还没有查到这笔交易，哈希已保存，"
            "请稍后刷新广播状态（链上数据同步有延迟）。"
        ),
        "unknown": "暂时无法确认源链广播状态，哈希已保存，请稍后重试。",
        "dropped_or_replaced": (
            "源账户 nonce 已推进但未查到该交易，"
            "交易可能已被替换或丢弃，请检查钱包重新广播。"
        ),
    }


def _broadcast_response(
    record: SwapSessionRecord,
    broadcast_status: BroadcastStatus | str | None = None,
    *,
    message: str | None = None,
) -> dict[str, Any]:
    payload = _jsonable(record)
    effective_status = broadcast_status or record.broadcast_status or record.stage or record.status
    payload["broadcast_status"] = effective_status
    if str(effective_status) == "broadcast_pending":
        # Expose immediate chain visibility without changing the persisted
        # business stage, which remains processing until provider completion.
        payload["status"] = effective_status
    if message is None and record.provider_order is not None:
        message = _broadcast_status_messages(record.language).get(
            str(effective_status),
            "兑换服务正在处理，目标链资产尚未确认到账。",
        )
    if message is not None:
        payload["message"] = message
    return payload


def _provider_order_with_persisted_metadata(
    order: ProviderOrder,
    pending_transaction: UnsignedTransaction | DepositOrder | None,
    quote: NormalizedQuote | None = None,
) -> ProviderOrder:
    """Carry restart-safe provider lookup metadata into the persisted order."""
    quote_payload = getattr(quote, "provider_payload", None)
    pending_payload = getattr(pending_transaction, "provider_payload", None)
    if not isinstance(quote_payload, Mapping):
        quote_payload = {}
    if not isinstance(pending_payload, Mapping):
        pending_payload = {}
    if not quote_payload and not pending_payload:
        return order
    merged = {**quote_payload, **pending_payload}
    merged.update(
        {
            str(key): value
            for key, value in order.provider_payload.items()
            if value not in (None, "")
        }
    )
    return order.model_copy(update={"provider_payload": merged})


async def _broadcast_transaction_confirmed(adapter: Any, tx_hash: str) -> bool:
    """Best-effort check that the source transaction is confirmed on-chain."""
    if hasattr(adapter, "get_transaction_receipt"):
        receipt = await adapter.get_transaction_receipt(tx_hash)
        if receipt is None:
            return False
        return receipt_success(receipt) is True
    if hasattr(adapter, "get_transaction_status"):
        status = await adapter.get_transaction_status(tx_hash)
        value = getattr(status, "value", status)
        return str(value).lower() == "confirmed"
    return False


async def _recover_swap_session_order(
    session: SwapSessionRecord,
    providers: Mapping[str, Any],
    session_store: SessionStore,
    execution_adapter: Any,
) -> SwapSessionRecord | None:
    """Register a provider order for a broadcast saved without one.

    Mirrors the graph's ``_recover_stale_broadcast_order`` so a passive
    ``GET /v1/swap/{session_id}`` can also self-heal a swap that was saved as
    ``not_propagated`` at broadcast time but confirmed on-chain later.
    """
    if session.quote is None or not session.broadcast_tx_hash:
        return None
    provider = providers.get(str(session.quote.provider))
    if provider is None or not hasattr(provider, "register_broadcast"):
        return None
    adapter = execution_adapter(str(session.quote.source_asset.chain)) if callable(
        execution_adapter
    ) else None
    if adapter is None:
        return None
    try:
        if not await _broadcast_transaction_confirmed(adapter, session.broadcast_tx_hash):
            return None
    except Exception:
        return None
    try:
        order = await provider.register_broadcast(
            str(session.quote.provider_reference),
            session.broadcast_tx_hash,
            quote=session.quote,
        )
    except Exception:
        return None
    order = _provider_order_with_persisted_metadata(
        order, session.pending_transaction, session.quote
    )
    try:
        return await session_store.update(
            session.session_id,
            expected_revision=session.revision,
            provider_order=order,
        )
    except SessionRevisionConflict:
        return await session_store.get(session.session_id) or session


async def _poll_swap_order_status(
    session: SwapSessionRecord,
    providers: Mapping[str, Any],
    session_store: SessionStore,
    *,
    execution_adapter: Any = None,
) -> SwapSessionRecord:
    """Refresh an in-flight swap order's status without a new chat turn.

    The graph's ``status_poll`` node advances ``order_status`` to its terminal
    state and promotes ``broadcast_status`` to ``confirmed``, but it only runs
    when the user sends a status message.  The App polls ``GET /v1/swap``
    passively, so a swap that finished provider-side stayed stuck at
    ``source_confirmed`` with ``order_status=null`` forever (Lif: “兑换订单已经
    到账很久了，现在 broadcast_status 还是 source_confirmed”).  Mirror that
    node's projection here: while an order is still live, query the provider
    once per read and persist the normalized status.
    """
    if session.quote is None:
        return session
    if session.provider_order is None:
        recovered = await _recover_swap_session_order(
            session, providers, session_store, execution_adapter
        )
        if recovered is None:
            return session
        session = recovered
    if session.provider_order is None:
        return session
    if session.status == "cancelled" or session.stage == "cancelled":
        return session
    existing = session.order_status
    if existing is not None and existing.status in _SWAP_ORDER_TERMINAL_STATES:
        return session
    provider = providers.get(str(session.quote.provider)) if session.quote else None
    if provider is None or not hasattr(provider, "get_status"):
        return session
    try:
        status = await provider.get_status(session.provider_order)
    except Exception:
        # Polling is best-effort: a provider hiccup must never fail the read.
        return session
    if status is None:
        return session
    provider_status = str(status.status)
    if existing is not None and existing.status == provider_status:
        return session
    changes: dict[str, Any] = {
        "order_status": status,
        "stage": provider_status,
        "status": provider_status,
    }
    if provider_status == "completed":
        # The source receipt alone is never swap completion; promote the
        # broadcast status only when the provider order itself completes.
        changes["broadcast_status"] = "confirmed"
    try:
        return await session_store.update(
            session.session_id,
            expected_revision=session.revision,
            **changes,
        )
    except SessionRevisionConflict:
        # A concurrent chat turn or broadcast advanced the projection; return
        # whatever is current so the client never sees stale data.
        return await session_store.get(session.session_id) or session


def _display_text(record: SwapSessionRecord, *keys: str) -> str | None:
    pending = record.pending_transaction
    display = getattr(pending, "display", None)
    if not isinstance(display, Mapping):
        return None
    for key in keys:
        value = display.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _display_int(record: SwapSessionRecord, *keys: str) -> int | None:
    value = _display_text(record, *keys)
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


async def _session_transfer_asset_fallback(
    session_store: SessionStore,
    user_id: str,
    chain: str,
    tx_hash: str,
) -> dict[str, Any] | None:
    """The moved asset of a session whose broadcast matches ``tx_hash``.

    A failed ERC-20/TRC-20 transfer often has no token layer in the explorer
    (native value 0), so the REST status would otherwise render the native
    coin's "0 ETH" row. The session remembers the intended transfer asset (with
    its icon) on ``record.asset``; return it so the card shows what actually
    moved. ``None`` when no matching session or asset exists.
    """
    normalized = tx_hash.strip().lower()
    chain_text = str(chain or "").upper()
    if not user_id or user_id == "anonymous":
        # No session scoping without a user identity. The broadcast hash is a
        # public on-chain identifier and the moved asset is public on-chain
        # data too, so annotating it from any session is safe — this is what
        # lets clients that do not send ``user_id`` (the demo, and App calls
        # without a Bearer token on AUTH_REQUIRED=false deployments) still
        # render the real amount/symbol instead of "0 ETH".
        try:
            session = await session_store.find_by_broadcast_tx_hash(normalized)
        except Exception:
            return None
        if session is None:
            return None
        asset = session.asset
        if not isinstance(asset, Mapping):
            return None
        session_chain = str(asset.get("chain") or "")
        if session_chain and session_chain.upper() != chain_text:
            return None
        return dict(asset)
    try:
        sessions = await session_store.list_for_user(user_id, limit=100)
    except Exception:
        return None
    for session in sessions:
        if not session.broadcast_tx_hash:
            continue
        if session.broadcast_tx_hash.strip().lower() != normalized:
            continue
        asset = session.asset
        if not isinstance(asset, Mapping):
            continue
        session_chain = str(asset.get("chain") or "")
        if session_chain and session_chain.upper() != chain_text:
            continue
        return dict(asset)
    return None


def _can_observe_broadcast(adapter: Any) -> bool:
    """Whether the adapter can classify a wallet-returned hash at all."""
    return hasattr(adapter, "get_transaction_receipt") or hasattr(
        adapter, "get_transaction_status"
    )


def _transaction_status_value(status: Any) -> str:
    return str(getattr(status, "value", status)).strip().lower()


async def _chain_broadcast_status(
    adapter: Any,
    tx_hash: str,
    *,
    sender: str | None = None,
    nonce: int | None = None,
) -> BroadcastStatus:
    """Classify a wallet-returned hash without treating RPC propagation as failure."""
    last_error: Exception | None = None
    observer_succeeded = False
    observer_error = False
    has_receipt = hasattr(adapter, "get_transaction_receipt")
    has_transaction = hasattr(adapter, "get_transaction")
    # UTXO chains (Bitcoin) and account-model chains have no EVM-style receipt,
    # so the status lookup is the only signal available for them.
    has_probe = hasattr(adapter, "get_transaction_status") and not has_receipt
    for attempt in range(_BROADCAST_RPC_ATTEMPTS):
        try:
            if has_receipt:
                receipt = await adapter.get_transaction_receipt(tx_hash)
                observer_succeeded = True
                if receipt is not None:
                    success = receipt_success(receipt)
                    if success is False:
                        return "failed"
                    return "confirmed" if success is True else "broadcast_pending"
            if has_transaction:
                transaction = await adapter.get_transaction(tx_hash)
                observer_succeeded = True
                if transaction is not None:
                    return "broadcast_pending"
            if has_probe:
                probe = _BROADCAST_STATUS_BY_PROBE.get(
                    _transaction_status_value(await adapter.get_transaction_status(tx_hash))
                )
                observer_succeeded = True
                # An inconclusive probe stays inconclusive instead of being
                # misreported as "the hash never reached the chain".
                return probe or "unknown"
        except Exception as exc:
            last_error = exc
            observer_error = True
        if attempt + 1 < _BROADCAST_RPC_ATTEMPTS:
            await asyncio.sleep(_BROADCAST_RPC_DELAY_SECONDS)
    if (
        sender
        and nonce is not None
        and hasattr(adapter, "get_transaction_count")
    ):
        try:
            current_nonce = await adapter.get_transaction_count(sender)
            observer_succeeded = True
            if current_nonce > nonce:
                return "dropped_or_replaced"
        except Exception as exc:
            last_error = exc
            observer_error = True
    if observer_error:
        return "unknown"
    if last_error is not None and not observer_succeeded:
        return "unknown"
    if not observer_succeeded:
        return "unknown"
    return "not_propagated"


def _qualified_hash(chain: str, tx_hash: str) -> bool:
    value = tx_hash.strip()
    if not value or any(c.isspace() for c in value):
        return False
    chain = chain.upper()
    if chain in {
        "EVM",
        "ETH",
        "BSC",
        "BASE",
        "POLYGON",
        "MATIC",
        "ARBITRUM",
        "ARB",
        "OPTIMISM",
        "OP",
    }:
        return (
            value.startswith("0x")
            and len(value) == 66
            and all(c in "0123456789abcdefABCDEF" for c in value[2:])
        )
    if chain in {"TRON", "TRX"}:
        return len(value) == 64 and all(c.isalnum() for c in value)
    if chain in {"BTC", "BITCOIN"}:
        return len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)
    if chain in {"SOLANA", "SOL"}:
        return 32 <= len(value) <= 128 and all(c.isalnum() for c in value)
    return False


def _okx_chain_indexes(chains: str) -> list[str]:
    values = [item.strip() for item in str(chains).split(",") if item.strip()]
    if not values:
        raise ValueError("at least one chain is required")
    indexes: list[str] = []
    for value in values:
        index = OKX_CHAIN_INDEX_BY_NAME.get(value.upper())
        if index is None:
            raise OkxWalletError(
                "OKX_CHAIN_UNSUPPORTED",
                "The requested chain is not supported.",
                details={"chain": value},
            )
        if index not in indexes:
            indexes.append(index)
    return indexes


def _interrupt_values(snapshot: Any) -> list[Any]:
    next_nodes = tuple(getattr(snapshot, "next", ()) or ())
    values: list[Any] = []
    for task in getattr(snapshot, "tasks", ()):
        for interruption in getattr(task, "interrupts", ()):
            values.append(getattr(interruption, "value", interruption))
    if values or not next_nodes:
        return values

    # LangGraph checkpoint implementations can expose a pending node through
    # ``snapshot.next`` while leaving ``task.interrupts`` empty.  Only infer
    # an interrupt when the node and persisted state agree; treating every
    # pending node as resumable would allow a confirmation action to mutate a
    # status poll or another unrelated graph task.
    state = getattr(snapshot, "values", None) or {}
    if not isinstance(state, Mapping):
        return values
    if "confirmation_wait" in next_nodes:
        confirmation = state.get("confirmation_state")
        if isinstance(confirmation, Mapping) and confirmation.get("status") == "requested":
            return [{"kind": "confirmation_required"}]
    if "swap_allowance" in next_nodes:
        authorization_stage = state.get("authorization_stage")
        response = state.get("response")
        response_stage = response.get("stage") if isinstance(response, Mapping) else None
        if authorization_stage == "approval_required" or response_stage == "approval_required":
            return [{"kind": "approval_required"}]
    return values


class _GraphInterruptConflict(Exception):
    """Raised when an API turn cannot safely resume the current checkpoint."""

    code = "GRAPH_INTERRUPT_CONFLICT"

    def __init__(self, *, kinds: set[str | None]) -> None:
        self.kinds = kinds
        rendered = ", ".join(sorted(kind or "unknown" for kind in kinds))
        super().__init__(
            "The graph is waiting for an action that this request cannot safely resume"
            f" ({rendered})."
        )


def _interrupt_kind(value: Any) -> str | None:
    if isinstance(value, Mapping):
        kind = value.get("kind")
        return str(kind) if kind is not None else None
    return None


def _resolve_graph_input(snapshot: Any, graph_input: dict[str, Any]) -> dict[str, Any] | Command:
    """Choose a normal graph input or an explicit resume command.

    ``snapshot.next`` only tells us that a checkpoint has work remaining; it
    does not mean that the work is an interrupt.  Resuming based on that flag
    can feed a status-poll or an unrelated task back into the wrong node.  We
    therefore require a known interrupt kind before constructing ``Command``.
    """

    values = _interrupt_values(snapshot)
    if not values:
        return graph_input
    kinds = {_interrupt_kind(value) for value in values}
    if kinds == {"approval_required"}:
        if not graph_input.get("approval_tx_hash"):
            raise _GraphInterruptConflict(kinds=kinds)
    elif kinds == {"confirmation_required"}:
        # A confirmation turn may contain either a yes/no answer or a revised
        # request.  The graph owns that interpretation, so both are resumed.
        pass
    else:
        raise _GraphInterruptConflict(kinds=kinds)
    return Command(resume=graph_input)


def _has_approval_interrupt(snapshot: Any) -> bool:
    """Return true only for the explicit wallet-approval interrupt."""
    return any(
        isinstance(value, Mapping) and value.get("kind") == "approval_required"
        for value in _interrupt_values(snapshot)
    )


def create_app(
    *,
    graph: Any = None,
    chain_registry: Any = None,
    providers: Mapping[str, Any] | None = None,
    wallet_provider: Any | None = None,
    explorer_provider: Any | None = None,
    execution_observer: Any | None = None,
    price_provider: Any | None = None,
    store: SessionStore | None = None,
    run_store: RunStore | None = None,
    lock_manager: LockManager | None = None,
    token_verifier: TokenVerifier | None = None,
    require_auth: bool = False,
    admin_token: str | None = None,
    model_registry: Any = None,
    metrics: Any = None,
    metadata_cache: Any = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            transports = list(application.state.transports)
            application.state.transports.clear()
            for transport in transports:
                close = getattr(transport, "aclose", None)
                if close is not None:
                    await close()
            for resource in (application.state.run_store, application.state.session_store):
                close = getattr(resource, "aclose", None)
                if close is not None:
                    await close()
            checkpoint_handle = getattr(application.state, "checkpointer_handle", None)
            close = getattr(checkpoint_handle, "aclose", None)
            if close is not None:
                await close()

    app = FastAPI(title="Wallet Agent", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            *(
                origin.strip().rstrip("/")
                for origin in str(os.getenv("CORS_ALLOW_ORIGINS", "")).split(",")
                if origin.strip()
            ),
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    session_store = store or InMemorySessionStore()
    local_runs: dict[str, dict[str, Any]] = {}
    event_store = run_store or InMemoryRunStore(local_runs)
    conversation_locks = lock_manager or InMemoryLockManager()
    provider_map = dict(providers or {})
    app.state.graph = graph
    app.state.chain_registry = chain_registry
    app.state.providers = provider_map
    app.state.wallet_provider = wallet_provider
    app.state.explorer_provider = explorer_provider
    app.state.execution_observer = execution_observer
    app.state.price_provider = price_provider
    app.state.session_store = session_store
    app.state.runs = local_runs
    app.state.run_store = event_store
    app.state.lock_manager = conversation_locks
    app.state.token_verifier = token_verifier
    app.state.require_auth = require_auth
    app.state.admin_token = admin_token
    app.state.model_registry = model_registry
    app.state.model_router = getattr(graph, "_model", None)
    app.state.metrics = metrics or getattr(graph, "metrics", None)
    app.state.metadata_cache = metadata_cache or getattr(graph, "metadata_cache", None)
    app.state.transports: list[Any] = []

    def graph_lock(thread_id: str) -> Any:
        return conversation_locks.lock(thread_id)

    async def append_run_event(run_id: str, event: Mapping[str, Any]) -> str:
        return await event_store.append(run_id, _jsonable(event))

    async def authorized_run_metadata(
        request: Request,
        run_id: str,
        *,
        fallback_user_id: str = "browser-demo",
        require_operator_when_auth_disabled: bool = False,
    ) -> dict[str, str] | None:
        """Authorize a run lookup and return its owner/link metadata.

        With authentication disabled, run inspection keeps the same local-demo
        semantics as the other owner-scoped endpoints. Once a verifier or
        ``require_auth`` is configured, only the run owner or the operator token
        may read it.
        """
        metadata = await event_store.get_metadata(run_id)
        if is_admin_token(request, admin_token):
            if metadata is None:
                raise HTTPException(status_code=404, detail="run not found")
            return metadata
        if token_verifier is None and not require_auth:
            if require_operator_when_auth_disabled and admin_token:
                raise HTTPException(status_code=401, detail="Bearer token is required")
            return metadata
        owner = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=fallback_user_id,
        )
        if (
            metadata is None
            or not metadata.get("owner_id")
            or metadata["owner_id"] != owner
        ):
            raise HTTPException(status_code=404, detail="run not found")
        return metadata

    async def read_run_events(run_id: str, *, max_events: int = 5_000) -> list[RunEvent] | None:
        """Read every event, paging the store because Redis caps each xread call."""
        events: list[RunEvent] = []
        cursor = "0"
        while len(events) < max_events:
            batch = await event_store.read(run_id, after_id=cursor)
            if batch is None:
                return None
            if not batch:
                break
            events.extend(batch)
            cursor = batch[-1].event_id
        return events

    def execution_adapter(chain: str) -> Any:
        observer = app.state.execution_observer
        if observer is not None and hasattr(observer, "adapter"):
            return observer.adapter(chain)
        registry = app.state.chain_registry
        if registry is None:
            raise RuntimeError("execution observer unavailable")
        return (
            registry.get_adapter(chain)
            if hasattr(registry, "get_adapter")
            else registry.get(chain)
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "details": {"errors": jsonable_encoder(exc.errors())},
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, Mapping) and detail.get("code") and detail.get("message"):
            body = {
                "code": detail["code"],
                "message": detail["message"],
            }
            if detail.get("details"):
                body["details"] = detail["details"]
        else:
            body = {
                "code": f"HTTP_{exc.status_code}",
                "message": str(detail),
            }
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(body),
            headers=exc.headers,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        if app.state.graph is None:
            raise HTTPException(status_code=503, detail="agent graph is not configured")
        try:
            if not await event_store.ready() or not await session_store.ready():
                raise RuntimeError("persistence dependency is unavailable")
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PERSISTENCE_UNAVAILABLE",
                    "message": "Runtime persistence is unavailable.",
                },
            ) from exc
        return {"status": "ready"}

    @app.get("/debug/metrics")
    async def debug_metrics(request: Request) -> dict[str, Any]:
        """Return bounded process-local timings; no prompts or credentials."""
        await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id="debug",
        )
        metrics_snapshot = (
            app.state.metrics.snapshot()
            if app.state.metrics is not None and hasattr(app.state.metrics, "snapshot")
            else {"model_calls": 0, "tool_calls": 0, "models": {}, "tools": {}}
        )
        cache_stats = (
            app.state.metadata_cache.stats()
            if app.state.metadata_cache is not None
            and hasattr(app.state.metadata_cache, "stats")
            else None
        )
        return {"metrics": metrics_snapshot, "metadata_cache": cache_stats}

    @app.get("/v1/agent/conversations")
    async def list_conversations(
        request: Request,
        user_id: str = "browser-demo",
        limit: int = 50,
        cursor: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        """Newest first. ``cursor`` pages backwards, ``q`` matches the title."""
        owner = (
            user_id
            if is_admin_token(request, admin_token)
            else await authenticated_user(
                request,
                verifier=token_verifier,
                required=require_auth,
                fallback_user_id=user_id,
            )
        )
        limit = max(1, min(limit, 100))
        sessions = await session_store.list_for_user(
            owner, limit=limit, cursor=_conversation_cursor(cursor), query=q
        )
        items = [_conversation_summary(session) for session in sessions]
        return {
            "conversations": _jsonable(items),
            "next_cursor": (
                items[-1]["updated_at"] if len(items) == limit and items else None
            ),
        }

    @app.get("/v1/agent/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str, request: Request, user_id: str = "browser-demo"
    ) -> dict[str, Any]:
        owner = (
            user_id
            if is_admin_token(request, admin_token)
            else await authenticated_user(
                request,
                verifier=token_verifier,
                required=require_auth,
                fallback_user_id=user_id,
            )
        )
        sessions = await session_store.list_for_user(owner, limit=100)
        session = next((item for item in sessions if item.thread_id == conversation_id), None)
        if session is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        graph = app.state.graph
        state: Mapping[str, Any] = {}
        if graph is not None and hasattr(graph, "aget_state"):
            snapshot = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
            state = getattr(snapshot, "values", None) or {}
        return {
            "conversation_id": conversation_id,
            "session_id": session.session_id,
            "session": _jsonable(session),
            "messages": _jsonable(state.get("conversation_history") or []),
            "latest_artifacts": _conversation_artifacts(state),
            "model_trace": _model_trace_for_state(state, model_registry),
        }

    @app.get("/v1/agent/runs/{run_id}")
    async def get_agent_run(
        run_id: str,
        request: Request,
        user_id: str = "browser-demo",
        event_limit: int | None = None,
    ) -> dict[str, Any]:
        """Read-only run triage: safe summary, linked session, and event history."""
        if event_limit is not None and not 1 <= event_limit <= 5_000:
            raise HTTPException(status_code=422, detail="event_limit must be between 1 and 5000")
        metadata = await authorized_run_metadata(
            request,
            run_id,
            fallback_user_id=user_id,
            require_operator_when_auth_disabled=True,
        )
        if metadata is None:
            raise HTTPException(status_code=404, detail="run not found")
        all_events = await read_run_events(run_id)
        if all_events is None:
            raise HTTPException(status_code=404, detail="run not found")
        returned_events = all_events[-event_limit:] if event_limit is not None else all_events
        terminal_state: Mapping[str, Any] = {}
        for stored_event in reversed(all_events):
            state = stored_event.payload.get("state")
            if isinstance(state, Mapping):
                terminal_state = state
                break

        owner_id = metadata.get("owner_id")
        conversation_id = metadata.get("conversation_id")
        session_id = metadata.get("session_id")
        session = None
        if session_id:
            session = await session_store.get(session_id)
        if session is None and owner_id:
            sessions = await session_store.list_for_user(owner_id, limit=100)
            session = next(
                (
                    item
                    for item in sessions
                    if conversation_id and item.thread_id == conversation_id
                ),
                None,
            )
        if session is not None and owner_id and session.user_id != owner_id:
            session = None
        artifacts = _conversation_artifacts(terminal_state) if terminal_state else None
        return sanitize_debug_value(
            {
                "run_id": run_id,
                "status": metadata.get("status")
                or await event_store.get_status(run_id)
                or "unknown",
                "owner_id": owner_id,
                "session_id": session.session_id if session is not None else session_id,
                "conversation_id": (
                    session.thread_id if session is not None else conversation_id
                ),
                "summary": build_run_summary(all_events, artifacts=artifacts),
                "session": _inspection_session_summary(session),
                "events": [
                    {"event_id": stored_event.event_id, **stored_event.payload}
                    for stored_event in returned_events
                ],
                "event_count": len(all_events),
                "returned_event_count": len(returned_events),
                "events_truncated": len(returned_events) < len(all_events),
            }
        )

    @app.delete("/v1/agent/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str, request: Request, user_id: str = "browser-demo"
    ) -> dict[str, Any]:
        owner = await authenticated_user(
            request, verifier=token_verifier, required=require_auth, fallback_user_id=user_id
        )
        sessions = await session_store.list_for_user(owner, limit=100)
        session = next((item for item in sessions if item.thread_id == conversation_id), None)
        if session is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        deleted = await session_store.delete(session.session_id, user_id=owner)
        graph = app.state.graph
        checkpointer = getattr(graph, "checkpointer", None)
        if checkpointer is not None and hasattr(checkpointer, "adelete_thread"):
            await checkpointer.adelete_thread(conversation_id)
        return {"conversation_id": conversation_id, "deleted": deleted}

    @app.patch("/v1/agent/conversations/{conversation_id}")
    async def update_conversation(
        conversation_id: str,
        payload: ConversationUpdateRequest,
        request: Request,
        user_id: str = "browser-demo",
    ) -> dict[str, Any]:
        owner = await authenticated_user(
            request, verifier=token_verifier, required=require_auth, fallback_user_id=user_id
        )
        sessions = await session_store.list_for_user(owner, limit=100)
        session = next((item for item in sessions if item.thread_id == conversation_id), None)
        if session is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        changes = {
            key: value
            for key, value in {"title": payload.title, "pinned": payload.pinned}.items()
            if value is not None
        }
        if not changes:
            raise HTTPException(status_code=422, detail="nothing to update")
        updated = await session_store.update(session.session_id, **changes)
        return {"conversation": _jsonable(_conversation_summary(updated))}

    async def persist_assistant_history(
        conversation_id: str, state: Mapping[str, Any], *, run_id: str | None = None
    ) -> None:
        """Persist response text and its immutable UI-card snapshot as one chat item."""
        graph = app.state.graph
        history_entry = _assistant_history_entry(state, turn_id=run_id)
        if history_entry is None or graph is None or not hasattr(graph, "aupdate_state"):
            return
        config = {"configurable": {"thread_id": conversation_id}}
        history = state.get("conversation_history") or []
        if hasattr(graph, "aget_state"):
            snapshot = await graph.aget_state(config)
            values = getattr(snapshot, "values", None) or {}
            history = values.get("conversation_history") or history
        last_item = history[-1] if history else None
        duplicate = (
            isinstance(last_item, Mapping)
            and last_item.get("role") == "assistant"
            and last_item.get("content") == history_entry["content"]
            and last_item.get("artifacts") == history_entry.get("artifacts")
        )
        if duplicate:
            return
        await graph.aupdate_state(config, {"conversation_history": [history_entry]})

    async def project_session(
        session_id: str | None,
        state: Mapping[str, Any],
        *,
        status: str | None = None,
        run_id: str | None = None,
    ) -> SwapSessionRecord | None:
        """Project only normalized graph values into the app-facing session index."""
        if not session_id:
            return None
        current = await session_store.get(session_id)
        if current is None:
            return None
        state = {str(key): _jsonable(value) for key, value in state.items()}
        changes: dict[str, Any] = {}
        operation_reset = bool(state.get("operation_reset"))
        historical_reference = state.get("operation_reference") == "history"
        if "operation_history" in state:
            changes["operation_history"] = [
                _jsonable(item)
                for item in (state.get("operation_history") or [])
                if isinstance(item, Mapping)
            ][-20:]
        if operation_reset:
            # A session is the projection of the current wallet operation.
            # Conversation history retains completed cards; stale swap
            # lifecycle fields must not contaminate a new transfer/swap.
            changes.update(
                quote=None,
                quote_candidates=[],
                selected_provider_reference=None,
                pending_transaction=None,
                provider_order=None,
                order_status=None,
                broadcast_tx_hash=None,
                broadcast_status=None,
                approval_transaction=None,
                approval_tx_hash=None,
                allowance_requirement=None,
                preflight=None,
                confirmation_state=None,
                gas_estimate=None,
                asset=None,
                chain_logo_url=None,
                last_error=None,
            )
        effective_broadcast_tx_hash = (
            state["broadcast_tx_hash"]
            if "broadcast_tx_hash" in state
            else (None if operation_reset else current.broadcast_tx_hash)
        )
        effective_broadcast_status = (
            state["broadcast_status"]
            if "broadcast_status" in state
            else (None if operation_reset else current.broadcast_status)
        )
        broadcast_lifecycle_active = not operation_reset and bool(
            effective_broadcast_tx_hash
            or current.provider_order
            or current.order_status
            or state.get("provider_orders")
            or state.get("status_snapshot")
        )
        selected = None if historical_reference else state.get("selected_quote")
        if selected:
            selected_reference = (
                selected.get("provider_reference")
                if isinstance(selected, Mapping)
                else getattr(selected, "provider_reference", None)
            )
            changes["quote"] = (
                selected
                if isinstance(selected, NormalizedQuote)
                else NormalizedQuote.model_validate(selected)
            )
            if selected_reference:
                changes["selected_provider_reference"] = str(selected_reference)
        candidates = [] if historical_reference else state.get("quote_candidates") or []
        if candidates:
            changes["quote_candidates"] = [
                item if isinstance(item, NormalizedQuote) else NormalizedQuote.model_validate(item)
                for item in candidates
            ]
        if "pending_transaction" in state and not historical_reference:
            pending = state["pending_transaction"]
            if pending is None:
                changes["pending_transaction"] = None
            elif isinstance(pending, (UnsignedTransaction, DepositOrder)):
                changes["pending_transaction"] = pending
            else:
                try:
                    changes["pending_transaction"] = UnsignedTransaction.model_validate(pending)
                except Exception:
                    changes["pending_transaction"] = DepositOrder.model_validate(pending)
        orders = {} if historical_reference else state.get("provider_orders") or {}
        if orders:
            raw_order = next(iter(orders.values()))
            changes["provider_order"] = (
                raw_order
                if isinstance(raw_order, ProviderOrder)
                else ProviderOrder.model_validate(raw_order)
            )
        snapshot = None if historical_reference else state.get("status_snapshot")
        if snapshot:
            changes["order_status"] = (
                snapshot
                if isinstance(snapshot, NormalizedOrderStatus)
                else NormalizedOrderStatus.model_validate(snapshot)
            )
        if "broadcast_tx_hash" in state and not historical_reference:
            changes["broadcast_tx_hash"] = (
                str(effective_broadcast_tx_hash) if effective_broadcast_tx_hash else None
            )
        if "broadcast_status" in state and not historical_reference:
            changes["broadcast_status"] = (
                str(effective_broadcast_status) if effective_broadcast_status else None
            )
        if not broadcast_lifecycle_active:
            if state.get("authorization_stage") is not None:
                changes["stage"] = state["authorization_stage"]
            elif state.get("task_stage") is not None:
                changes["stage"] = state["task_stage"]
        response = state.get("response") or {}
        if historical_reference and response.get("kind") == "swap_status":
            history = [
                dict(item)
                for item in (current.operation_history or [])
                if isinstance(item, Mapping)
            ]
            task_id = state.get("operation_reference_task_id")
            updated = False
            for item in history:
                if task_id and item.get("task_id") != task_id:
                    continue
                if isinstance(response.get("status"), Mapping):
                    item["status_snapshot"] = _jsonable(response["status"])
                if response.get("provider_status"):
                    item["provider_status"] = response["provider_status"]
                if response.get("broadcast_status"):
                    item["broadcast_status"] = response["broadcast_status"]
                updated = True
                break
            if updated:
                changes["operation_history"] = history[-20:]
        if response.get("kind") == "transfer_prepare":
            changes.update(
                broadcast_tx_hash=None,
                broadcast_status=None,
                stage="transfer_ready",
            )
            broadcast_lifecycle_active = False
        if response.get("kind") == "swap_status" and not historical_reference:
            raw_status = response.get("status")
            order_state = (
                raw_status.get("status") if isinstance(raw_status, Mapping) else raw_status
            )
            if order_state:
                changes["stage"] = str(order_state)
            if response.get("broadcast_status") == "broadcast_pending":
                changes["stage"] = "broadcast_pending"
            if state.get("provider_orders") or state.get("broadcast_tx_hash"):
                # Once a swap has been broadcast, old checkpoint values are no
                # longer actionable and must not make clients render signing cards.
                changes["approval_transaction"] = None
                changes["pending_transaction"] = None
        status_snapshot = state.get("status_snapshot")
        if (
            broadcast_lifecycle_active
            and not historical_reference
            and isinstance(status_snapshot, Mapping)
            and status_snapshot.get("status")
            and str(status_snapshot["status"])
            in {"completed", "refunded", "failed", "timed_out", "kyc_required", "cancelled"}
        ):
            # A chat arrival check can recover the provider order and persist a
            # terminal status_snapshot; mirror it onto the session stage so the
            # App status card stops showing 交易中.
            changes["stage"] = str(status_snapshot["status"])
        if response.get("kind") == "cancelled" and not broadcast_lifecycle_active:
            changes.update(
                status="cancelled",
                stage="cancelled",
                pending_transaction=None,
                approval_transaction=None,
                approval_tx_hash=None,
                allowance_requirement=None,
                confirmation_state=None,
                gas_estimate=None,
                asset=None,
                chain_logo_url=None,
            )
        for field in ("approval_transaction", "approval_tx_hash", "allowance_requirement"):
            if state.get(field) is not None:
                changes[field] = state[field]
        if "preflight" in state:
            # Mirror the cleared value too: a turn that dropped the stale
            # unsigned transaction (e.g. a balance query after a prepared
            # transfer) must also revoke the old preflight from the session,
            # or the App keeps rendering the obsolete confirmation card.
            changes["preflight"] = state["preflight"]
        # The unsigned-transaction / swap-status cards show the moved asset; keep
        # its identity (icon included) on the session so the REST clients get the
        # same row the SSE frame carried. Gated on a pending transaction so a
        # read-only price answer cannot overwrite the operation's asset.
        if changes.get("pending_transaction") is not None:
            response_asset = response.get("asset")
            if isinstance(response_asset, Mapping):
                changes["asset"] = _jsonable(response_asset)
            if response.get("chain_logo_url") is not None:
                changes["chain_logo_url"] = response["chain_logo_url"]
        if state.get("confirmation_state") is not None:
            changes["confirmation_state"] = _jsonable(state["confirmation_state"])
        if state.get("swap_gas_estimate") is not None:
            changes["gas_estimate"] = _jsonable(state["swap_gas_estimate"])
        if response.get("kind") == "error" and not broadcast_lifecycle_active:
            # An unsuccessful prepare/preflight must revoke every unsigned
            # action projected by an older turn.  Otherwise clients can still
            # render and submit a transaction that no longer matches the
            # current request.
            changes.update(
                pending_transaction=None,
                approval_transaction=None,
                approval_tx_hash=None,
                allowance_requirement=None,
                confirmation_state=None,
                gas_estimate=None,
                asset=None,
                chain_logo_url=None,
                stage="failed",
            )
            errors = response.get("errors") or []
            if errors and isinstance(errors[0], Mapping):
                changes["last_error"] = _jsonable(errors[0])
        if status:
            changes["status"] = status
        if run_id:
            changes["last_run_id"] = run_id
        if not changes:
            return current
        try:
            return await session_store.update(
                session_id, expected_revision=current.revision, **changes
            )
        except SessionRevisionConflict:
            # A concurrent idempotent endpoint may have advanced the
            # projection.  Re-read and apply only this projection's fields so
            # unrelated state is never rolled back.
            latest = await session_store.get(session_id)
            if latest is None:
                return None
            return await session_store.update(
                session_id, expected_revision=latest.revision, **changes
            )

    async def persist_approval_projection(
        session_id: str,
        session: SwapSessionRecord,
        quote: NormalizedQuote,
    ) -> SwapSessionRecord:
        requirement = quote.allowance_requirement
        if requirement is None:
            return session
        try:
            adapter = execution_adapter(requirement.token.chain)
        except Exception:
            return session
        tx = adapter.build_erc20_approve(
            token=requirement.token,
            owner=requirement.owner,
            spender=requirement.spender,
            amount_raw=requirement.required_amount_raw,
        )
        # EVM adapters build approvals synchronously; account-model chains
        # (TRON) build them over the node and return a coroutine.
        if hasattr(tx, "__await__"):
            tx = await tx
        approval = {
            "chain": requirement.token.chain,
            "chain_id": requirement.token.chain_id,
            "to": tx.to,
            "data": tx.data,
            "value": tx.value,
            "token": requirement.token.model_dump(mode="json"),
            "owner": requirement.owner,
            "spender": requirement.spender,
            "amount_raw": requirement.required_amount_raw,
            "expires_at": None,
        }
        return await session_store.update(
            session_id,
            status="approval_required",
            stage="approval_required",
            approval_transaction=approval,
            allowance_requirement=requirement.model_dump(mode="json"),
        )

    async def run_graph(
        run_id: str,
        input_state: dict[str, Any],
        config: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> None:
        await event_store.set_status(run_id, "running")
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        lock = graph_lock(thread_id)
        acquired = False
        try:
            await lock.__aenter__()
            acquired = True
            if app.state.graph is None:
                result = input_state
                await append_run_event(
                    run_id,
                    {"event": "complete", "state": _stream_state(result)}
                )
            else:

                router = getattr(app.state.graph, "_model", None)

                def collect_model_trace() -> list[dict[str, Any]]:
                    if router is None or not hasattr(router, "consume_model_trace"):
                        return []
                    return [_jsonable(item) for item in router.consume_model_trace()]

                async def get_snapshot() -> Any:
                    if hasattr(app.state.graph, "aget_state"):
                        return await app.state.graph.aget_state(config)
                    return app.state.graph.get_state(config)

                async def execute(graph_input: dict[str, Any]) -> dict[str, Any]:
                    snapshot = await get_snapshot()
                    request = (
                        graph_input.get("request")
                        if isinstance(graph_input, Mapping)
                        else None
                    )
                    message = request.get("message") if isinstance(request, Mapping) else None
                    if (
                        isinstance(graph_input, dict)
                        and message
                    ):
                        # A broadcasted swap may leave the old confirmation
                        # interrupt in the checkpoint until the next graph
                        # execution.  Once the wallet has registered a tx,
                        # an ordinary conversational turn must not resume that
                        # stale interrupt and replay quote/prepare nodes.  A
                        # subsequent explicit swap_status turn still routes
                        # through the normal provider status path.
                        broadcast_lifecycle_active = bool(
                            graph_input.get("broadcast_tx_hash")
                            or graph_input.get("provider_orders")
                        )
                        if broadcast_lifecycle_active:
                            # Explicitly clear turn-scoped routing values from
                            # the checkpoint.  A previous confirmation/prepare
                            # run can otherwise leave ``forced_intent`` or
                            # ``intent`` behind even when this request carries
                            # a fresh message, causing the old swap path to be
                            # replayed before the model sees the new turn.
                            if hasattr(app.state.graph, "aupdate_state"):
                                await app.state.graph.aupdate_state(
                                    config,
                                    {
                                        "forced_intent": None,
                                        "intent": None,
                                        "supervisor_output": None,
                                        "supervisor_decision": None,
                                        "response_action": None,
                                        "response": None,
                                        "status_snapshot": None,
                                    },
                                )
                                snapshot = await get_snapshot()
                        else:
                            graph_input = _resolve_graph_input(snapshot, graph_input)
                    async for streamed in app.state.graph.astream(
                        graph_input,
                        config=config,
                        stream_mode=["updates", "custom"],
                    ):
                        if (
                            isinstance(streamed, tuple)
                            and len(streamed) == 2
                            and streamed[0] in {"updates", "custom"}
                        ):
                            mode, event = streamed
                        else:
                            # Compatibility with graph doubles and older graph
                            # implementations that return update chunks directly.
                            mode, event = "updates", streamed
                        # Keep transport semantics explicit: custom writer payloads
                        # are progress, while graph state responses are user-facing
                        # only when they contain a typed response object.
                        if mode == "updates" and isinstance(event, Mapping):
                            # Project node updates exactly like the terminal
                            # frame: LangGraph hands over raw node payloads, and
                            # for the ReAct nodes those are entire message lists
                            # (system prompt included). A node update that
                            # carries nothing the App renders is not worth a
                            # frame at all.
                            event = _stream_state(event)
                            if not event:
                                continue
                            # The projection strips top-level private keys, but
                            # the ReAct loop publishes its messages nested under
                            # the node name (react_entry / react_agent /
                            # react_tools), so those lists would still ride
                            # along. The App only logs these node frames — none
                            # of the remaining fields is rendered — so drop the
                            # frame outright.
                            if (
                                len(event) == 1
                                and next(iter(event)) in _STREAM_REACT_NODES
                            ):
                                continue
                        event_name = "progress" if mode == "custom" else (
                            "response"
                            if isinstance(event, Mapping)
                            and isinstance(event.get("response"), Mapping)
                            else "update"
                        )
                        await append_run_event(
                            run_id,
                            {"event": event_name, "data": event}
                        )
                        trace = collect_model_trace()
                        if trace:
                            await append_run_event(
                                run_id,
                                {"event": "model", "data": {"model_trace": trace}},
                            )
                    snapshot = await get_snapshot()
                    final_state = dict(snapshot.values)
                    trace = collect_model_trace()
                    if trace:
                        prior_trace = [
                            *(_jsonable(final_state.get("model_trace") or [])),
                            *trace,
                        ]
                        final_state["model_trace"] = prior_trace
                        if hasattr(app.state.graph, "aupdate_state"):
                            await app.state.graph.aupdate_state(
                                config, {"model_trace": prior_trace}
                            )
                            snapshot = await get_snapshot()
                            final_state = dict(snapshot.values)
                    return final_state
                result = await execute(input_state)
                # A swap turn owns a business session. Once quotes are available,
                # continue the same thread into the confirmation interrupt so the
                # mobile client has one stable session_id for the full lifecycle.
                # The lifecycle path selects a quote explicitly; the ReAct path
                # defaults to the best quote (design: one channel straight to the
                # confirmation card), so both continue here automatically.
                response_kind = (result.get("response") or {}).get("kind")
                if (
                    session_id
                    and result.get("selected_quote")
                    and result.get("quote_candidates")
                    and not result.get("pending_transaction")
                    and response_kind in {"swap_quote", "react"}
                ):
                    await project_session(session_id, result, status="quoted", run_id=run_id)
                    # The quote and its cards are a completed user-facing step.
                    # Preserve it before the automatic confirmation execution
                    # replaces ``response`` in the same checkpoint.
                    await persist_assistant_history(thread_id, result, run_id=run_id)
                    fresh_swap = bool(
                        input_state.get("swap_request")
                        or result.get("fresh_swap_projected")
                        or any(
                            str(item.get("tool") or "") == "get_swap_quote"
                            and isinstance(item.get("result"), Mapping)
                            and item["result"].get("ok")
                            and item["result"].get("quotes")
                            for item in (
                                (result.get("response") or {}).get("react_results") or []
                            )
                        )
                    )
                    prepare_input: dict[str, Any] = {
                        "request": result.get("request"),
                        "wallet_context": result.get("wallet_context"),
                        "conversation_state": result.get("conversation_state"),
                        "active_task": result.get("active_task"),
                        "swap_draft": result.get("swap_draft"),
                        "intent": "swap_prepare",
                        "forced_intent": "swap_prepare",
                        "swap_request": result.get("swap_request"),
                        "selected_quote": result.get("selected_quote"),
                        "swap_session": {"session_id": session_id},
                        # ``route_after_intent`` must not let a stale
                        # broadcast_tx_hash hijack a *fresh* swap_prepare into
                        # register_broadcast/status_poll, and a fresh prepare
                        # must not carry the previous broadcast's lifecycle
                        # fields into the new confirmation card.
                        "fresh_swap": fresh_swap,
                    }
                    if fresh_swap:
                        prepare_input.update(
                            {
                                "operation_reset": True,
                                "broadcast_tx_hash": None,
                                "broadcast_status": None,
                                "provider_orders": None,
                                "provider_order_ids": None,
                                "status_snapshot": None,
                            }
                        )
                    result = await execute(prepare_input)
                snapshot = await get_snapshot()
                result = snapshot.values
                await persist_assistant_history(thread_id, result, run_id=run_id)
                snapshot = await get_snapshot()
                result = snapshot.values
                interrupted = bool(getattr(snapshot, "tasks", ())) and bool(
                    getattr(snapshot, "next", ())
                )
                if interrupted or "__interrupt__" in result:
                    # Project the session index *before* the terminal frame: the
                    # client reacts to ``action_required`` immediately (confirm,
                    # or read the session), and a session that is not yet
                    # updated would look like a missing swap.
                    await project_session(
                        session_id, result, status="awaiting_confirmation", run_id=run_id
                    )
                    await append_run_event(
                        run_id,
                        {"event": "action_required", "state": _stream_state(result)}
                    )
                    await event_store.set_status(run_id, "awaiting_confirmation")
                else:
                    response_kind = (result.get("response") or {}).get("kind")
                    session_status = None
                    if response_kind == "transfer_prepare":
                        session_status = "transfer_ready"
                    elif response_kind == "swap_status":
                        raw_status = (result.get("response") or {}).get("status")
                        session_status = (
                            raw_status.get("status")
                            if isinstance(raw_status, Mapping)
                            else raw_status
                        )
                        if (
                            session_status == "processing"
                            and result.get("broadcast_status") == "broadcast_pending"
                        ):
                            session_status = "broadcast_pending"
                    if response_kind == "error":
                        active_kind = (result.get("active_task") or {}).get("kind")
                        if result.get("intent") == "swap_quote" or active_kind == "swap":
                            session_status = "quote_failed"
                        elif result.get("intent") == "transfer" or active_kind == "transfer":
                            session_status = "failed"
                    elif response_kind == "swap_quote" and not result.get("selected_quote"):
                        session_status = "quoted"
                    elif response_kind == "react" and result.get("quote_candidates"):
                        # A chat arrival check may recover and report a terminal
                        # order state (e.g. completed). That must win over the
                        # generic "quoted" projection so the App status card
                        # stops showing 交易中.
                        react_swap_status = next(
                            (
                                item["result"]
                                for item in (result.get("react_results") or [])
                                if item.get("tool") == "query_swap_status"
                                and item.get("result")
                                and item["result"].get("provider_order_id")
                            ),
                            None,
                        )
                        if react_swap_status and react_swap_status.get("status"):
                            session_status = str(react_swap_status.get("status"))
                        else:
                            session_status = "quoted"
                    elif response_kind == "clarification":
                        active_task = result.get("active_task") or {}
                        task_stage = result.get("task_stage") or active_task.get("stage")
                        if task_stage == "collecting_parameters":
                            session_status = "collecting_parameters"
                    if result.get("operation_reference") == "history":
                        # A historical status lookup updates only the archived
                        # operation snapshot; it must not replace the current
                        # transfer/swap session status.
                        session_status = None
                    await project_session(session_id, result, status=session_status, run_id=run_id)
                    # Terminal frame last, for the same reason as above: a client
                    # that reads or selects a quote the moment the run ends must
                    # find the session already projected.
                    await append_run_event(
                        run_id,
                        {"event": "complete", "state": _stream_state(result)}
                    )
                    await event_store.set_status(run_id, "complete")
            if await event_store.get_status(run_id) == "running":
                await event_store.set_status(run_id, "complete")
        except _GraphInterruptConflict as exc:
            error = AgentError(
                code=exc.code,
                message=str(exc),
                details={
                    "interrupt_kinds": sorted(kind or "unknown" for kind in exc.kinds)
                },
            ).model_dump(mode="json")
            await append_run_event(
                run_id,
                {
                    "event": "error",
                    "error": error,
                }
            )
            await event_store.set_status(run_id, "failed")
        except Exception as exc:  # errors are returned without exception internals
            error = AgentError(code="AGENT_EXECUTION_ERROR", message=str(exc)).model_dump(
                mode="json"
            )
            await append_run_event(
                run_id,
                {
                    "event": "error",
                    "error": error,
                }
            )
            await event_store.set_status(run_id, "failed")
            if session_id:
                current = await session_store.get(session_id)
                if current is not None:
                    try:
                        await session_store.update(
                            session_id,
                            expected_revision=current.revision,
                            status="failed",
                            stage="failed",
                            last_run_id=run_id,
                            last_error=error,
                        )
                    except SessionRevisionConflict:
                        # A concurrent request may have advanced the session;
                        # never let cleanup hide the original graph error.
                        pass
        finally:
            if acquired:
                await lock.__aexit__(None, None, None)

    @app.post("/v1/agent/turn")
    async def turn(request: Request, payload: TurnRequest) -> dict[str, Any]:
        if payload.user_id is None and token_verifier is None and not require_auth:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed.",
                    "details": {"errors": [{"loc": ["body", "user_id"], "msg": "Field required"}]},
                },
            )
        user_id = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=payload.user_id,
        )
        if model_registry is not None:
            try:
                selected_model_id = model_registry.validate(payload.model_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "MODEL_NOT_ALLOWED",
                        "message": str(exc),
                        "details": {"allowed_models": list(model_registry.model_ids)},
                    },
                ) from exc
        else:
            selected_model_id = payload.model_id
        existing_session = None
        if payload.session_id:
            existing_session = await session_store.get(payload.session_id)
            if existing_session is None or existing_session.user_id != user_id:
                raise HTTPException(status_code=404, detail="swap session not found")
            if payload.conversation_id and payload.conversation_id != existing_session.thread_id:
                raise HTTPException(
                    status_code=409, detail="conversation_id does not match session"
                )
        conversation_id = payload.conversation_id or (
            existing_session.thread_id if existing_session else str(uuid.uuid4())
        )
        run_id = str(uuid.uuid4())
        raw_agent_test_intent = payload.metadata.get("agent_test_intent")
        if raw_agent_test_intent is not None and (
            not isinstance(raw_agent_test_intent, str)
            or raw_agent_test_intent not in _AGENT_TEST_INTENTS
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_AGENT_TEST_INTENT",
                    "message": "agent_test_intent is not supported.",
                    "details": {"allowed_intents": sorted(_AGENT_TEST_INTENTS)},
                },
            )
        raw_swap_request = payload.metadata.get("swap_request") or payload.metadata.get("swap")
        raw_transfer_request = payload.metadata.get("transfer_request") or payload.metadata.get(
            "transfer"
        )
        raw_price_request = payload.metadata.get("price_request") or payload.metadata.get("price")
        raw_transaction_query = payload.metadata.get("transaction_query") or payload.metadata.get(
            "transaction"
        )
        raw_swap_query = payload.metadata.get("swap_query") or payload.metadata.get(
            "swap_order"
        )
        raw_portfolio_query = payload.metadata.get("portfolio_query") or payload.metadata.get(
            "portfolio"
        )
        raw_gas_query = payload.metadata.get("gas_request") or payload.metadata.get("gas")
        raw_asset_query = payload.metadata.get("asset_query") or payload.metadata.get("assets")
        session_id = payload.session_id
        # Every turn gets a session record so read-only conversations (asset,
        # balance, receive-address questions) also show up in the chat history.
        if session_id is None:
            session_id = str(uuid.uuid4())
        if session_id:
            if existing_session is None:
                await session_store.save(
                    SwapSessionRecord(
                        session_id=session_id,
                        user_id=user_id,
                        thread_id=conversation_id,
                        title=_conversation_title(payload.message),
                        # Keep the Chinese product default when the first
                        # message carries no language signal (amounts, hashes).
                        language=detect_message_language(payload.message) or "zh",
                    )
                )
            elif existing_session.thread_id != conversation_id:
                raise HTTPException(status_code=409, detail="session thread cannot be changed")
        wallet_context: dict[str, Any] = {}
        if payload.address:
            wallet_context["address"] = payload.address
        if payload.chain:
            wallet_context["chain"] = payload.chain
        if payload.supported_networks is not None:
            wallet_context["supported_networks"] = payload.supported_networks
        if payload.wallet_accounts is not None:
            wallet_context["wallet_accounts"] = [
                # Absent signing fields stay absent: an App that does not report
                # them must forward exactly what it sent before.
                item.model_dump(mode="json", exclude_none=True)
                for item in payload.wallet_accounts
            ]
        metadata_chain_id = payload.metadata.get("wallet_chain_id")
        if metadata_chain_id is not None:
            wallet_context["chain_id"] = metadata_chain_id
        input_state = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "model_id": selected_model_id,
            "request": payload.model_dump(mode="json"),
            "messages": [{"role": "user", "content": payload.message}],
            "conversation_history": [{"role": "user", "content": payload.message}],
            # Do not let a previous checkpoint response be projected as this
            # turn's response or lifecycle stage.
            "response": None,
            # API-forced intents are turn-scoped. Explicitly overwrite any
            # value left in the LangGraph checkpoint by select/continue APIs.
            "forced_intent": None,
            # Turn-scoped signal. A graph node sets it when this message starts
            # a new business operation in the same conversation.
            "operation_reset": False,
        }
        if existing_session is not None:
            if existing_session.operation_history:
                input_state["operation_history"] = _jsonable(existing_session.operation_history)
            if existing_session.quote is not None:
                input_state["selected_quote"] = existing_session.quote.model_dump(mode="json")
            if existing_session.selected_provider_reference:
                input_state["selected_provider_reference"] = (
                    existing_session.selected_provider_reference
                )
            if existing_session.approval_tx_hash is not None:
                input_state["approval_tx_hash"] = existing_session.approval_tx_hash
            if existing_session.approval_transaction is not None:
                input_state["approval_transaction"] = existing_session.approval_transaction
            if existing_session.allowance_requirement is not None:
                input_state["allowance_requirement"] = existing_session.allowance_requirement
            if existing_session.pending_transaction is not None:
                pending_transaction = existing_session.pending_transaction
                input_state["pending_transaction"] = pending_transaction.model_dump(mode="json")
            if existing_session.broadcast_tx_hash:
                input_state["broadcast_tx_hash"] = existing_session.broadcast_tx_hash
                broadcast_status = (
                    existing_session.broadcast_status
                    or existing_session.stage
                    or existing_session.status
                )
                if broadcast_status in {
                    "broadcast_seen",
                    "broadcast_pending",
                    "not_propagated",
                    "source_confirmed",
                    "confirmed",
                    "failed",
                    "dropped_or_replaced",
                    "unknown",
                }:
                    input_state["broadcast_status"] = broadcast_status
        if existing_session is not None and existing_session.provider_order is not None:
            # Backfill orders created before provider metadata was persisted,
            # so an existing conversation also survives a process restart.
            order = _provider_order_with_persisted_metadata(
                existing_session.provider_order,
                existing_session.pending_transaction,
                existing_session.quote,
            )
            input_state.update(
                {
                    "provider_orders": {
                        str(order.provider): order.model_dump(mode="json")
                    },
                    "provider_order_ids": {
                        str(order.provider): order.provider_order_id
                    },
                    "broadcast_tx_hash": existing_session.broadcast_tx_hash,
                    "poll_attempts": 0,
                    "status_snapshot": None,
                    # A registered provider order proves signing is finished.
                    # Clear stale checkpoint actions from the next SSE state.
                    "approval_transaction": None,
                    "pending_transaction": None,
                    "authorization_stage": None,
                }
            )
        if wallet_context:
            input_state["wallet_context"] = wallet_context
        if raw_swap_request is not None:
            input_state["swap_request"] = raw_swap_request
        if raw_transfer_request is not None:
            input_state["forced_intent"] = "transfer"
            input_state["intent"] = "transfer"
            input_state["transfer_request"] = raw_transfer_request
        if raw_price_request is not None:
            input_state["forced_intent"] = "price_query"
            input_state["intent"] = "price_query"
            input_state["price_request"] = raw_price_request
        if raw_transaction_query is not None:
            input_state["forced_intent"] = "transaction_status"
            input_state["intent"] = "transaction_status"
            input_state["transaction_query"] = raw_transaction_query
        if raw_swap_query is not None:
            # The App opened the assistant from a swap order detail page with
            # (order_id, tx_hash, chain). Surface it into state so the ReAct
            # entry can scope the prompt and toolset (no recent-transaction
            # history card over an order-detail analysis). Unlike
            # transaction_query we do NOT force a lifecycle intent: starting a
            # fresh swap collection over an existing order is exactly the
            # misrouting the swap context exists to prevent.
            input_state["swap_query"] = raw_swap_query
        if raw_portfolio_query is not None:
            input_state["forced_intent"] = "portfolio_query"
            input_state["intent"] = "portfolio_query"
            input_state["portfolio_request"] = raw_portfolio_query
        if raw_gas_query is not None:
            input_state["forced_intent"] = "gas_check"
            input_state["intent"] = "gas_check"
            input_state["gas_request"] = raw_gas_query
        if raw_asset_query is not None:
            input_state["forced_intent"] = "asset_discovery"
            input_state["intent"] = "asset_discovery"
            input_state["asset_query"] = raw_asset_query
        if raw_agent_test_intent is not None:
            input_state["forced_intent"] = raw_agent_test_intent
            input_state["intent"] = raw_agent_test_intent
        config = {"configurable": {"thread_id": conversation_id}}
        await event_store.create(
            run_id,
            owner_id=user_id,
            metadata={
                "session_id": session_id,
                "conversation_id": conversation_id,
            },
        )
        app.state.runs.setdefault(run_id, {})
        task = asyncio.create_task(run_graph(run_id, input_state, config, session_id=session_id))
        app.state.runs[run_id]["task"] = task
        response = {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "status": "running",
        }
        if session_id:
            response["session_id"] = session_id
        return response

    @app.get("/v1/agent/stream/{run_id}")
    async def stream(run_id: str, request: Request) -> StreamingResponse:
        await authorized_run_metadata(request, run_id)

        async def events() -> AsyncIterator[str]:
            cursor = (
                request.headers.get("last-event-id")
                or request.query_params.get("last_event_id")
                or "0"
            )
            while True:
                if await request.is_disconnected():
                    return
                event_list = await event_store.read(
                    run_id,
                    after_id=cursor,
                    block_ms=1_000,
                )
                if event_list is None:
                    yield (
                        "event: error\ndata: "
                        '{"code":"RUN_NOT_FOUND","message":"Run not found."}\n\n'
                    )
                    return
                for stored_event in event_list:
                    event = stored_event.payload
                    cursor = stored_event.event_id
                    kind = event.get("event", "update")
                    data = json.dumps(_jsonable(event), ensure_ascii=True)
                    yield f"id: {cursor}\nevent: {kind}\ndata: {data}\n\n"
                status = await event_store.get_status(run_id)
                if status in TERMINAL_RUN_STATUSES:
                    return

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    async def owned_session(session_id: str, user_id: str) -> SwapSessionRecord:
        session = await session_store.get(session_id)
        if session is None or session.user_id != user_id:
            raise HTTPException(status_code=404, detail="swap session not found")
        return session

    @app.post("/v1/swap/{session_id}/confirm")
    async def confirm(request: Request, session_id: str, payload: ConfirmRequest) -> dict[str, Any]:
        user_id = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=payload.user_id,
        )
        session = await owned_session(session_id, user_id)
        if not payload.approved:
            graph = app.state.graph
            if graph is not None:
                try:
                    async with graph_lock(session.thread_id):
                        result = await graph.ainvoke(
                            Command(resume={"approved": False}),
                            config={"configurable": {"thread_id": session.thread_id}},
                        )
                        await persist_assistant_history(session.thread_id, result)
                    updated = await project_session(session_id, result, status="cancelled")
                    if updated is not None:
                        return _jsonable(updated)
                except Exception as exc:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "CONFIRMATION_CANCEL_FAILED",
                            "message": "无法取消当前确认流程，请稍后重试。",
                            "details": {"error": str(exc)},
                        },
                    ) from exc
            return _jsonable(await session_store.update(session_id, status="cancelled"))
        if session.pending_transaction is not None:
            return _jsonable(session)
        graph = app.state.graph
        if graph is None:
            return _jsonable(await session_store.update(session_id, status="confirmed"))
        config = {"configurable": {"thread_id": session.thread_id}}
        async with graph_lock(session.thread_id):
            snapshot_before = (
                await graph.aget_state(config)
                if hasattr(graph, "aget_state")
                else graph.get_state(config)
            )
            interrupt_values = _interrupt_values(snapshot_before)
            interrupt_kinds = {_interrupt_kind(value) for value in interrupt_values}
            if interrupt_kinds != {"confirmation_required"}:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "CONFIRMATION_NOT_PENDING",
                        "message": "当前兑换确认状态已变化，请刷新后再试。",
                        "details": {
                            "interrupt_kinds": sorted(
                                kind or "unknown" for kind in interrupt_kinds
                            )
                        },
                    },
                )
            result = await graph.ainvoke(
                Command(resume={"approved": True}),
                config=config,
            )
            await persist_assistant_history(session.thread_id, result)
            if hasattr(graph, "aget_state"):
                snapshot = await graph.aget_state(config)
            else:
                snapshot = graph.get_state(config)
            values = getattr(snapshot, "values", None) or {}
            confirmation_state = values.get("confirmation_state") or result.get(
                "confirmation_state"
            )
            if (
                isinstance(confirmation_state, Mapping)
                and confirmation_state.get("status") == "requested"
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "CONFIRMATION_RESUME_NOOP",
                        "message": "确认请求没有推进，请刷新兑换状态后重试。",
                    },
                )
            if _has_approval_interrupt(snapshot):
                updated = await project_session(
                    session_id, result, status="approval_required"
                )
                if updated is not None and session.quote is not None:
                    updated = await persist_approval_projection(
                        session_id, updated, session.quote
                    )
                return _jsonable(updated or result)
        response = result.get("response") or {}
        errors = response.get("errors") or []
        expired = any(item.get("code") == "CONFIRMATION_EXPIRED" for item in errors)
        authorization_stage = result.get("authorization_stage")
        status = authorization_stage or (
            "confirmation_expired" if expired else "confirmed"
        )
        updated = await project_session(session_id, result, status=status)
        if updated is not None and authorization_stage:
            updated = await session_store.update(
                session_id,
                status=authorization_stage,
                stage=authorization_stage,
            )
        payload = _jsonable(updated or result)
        if response.get("kind") == "error":
            payload["response"] = _jsonable(response)
        return payload

    @app.post("/v1/swap/{session_id}/cancel")
    async def cancel(request: Request, session_id: str, payload: CancelRequest) -> dict[str, Any]:
        user_id = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=payload.user_id,
        )
        await owned_session(session_id, user_id)
        return _jsonable(await session_store.update(session_id, status="cancelled"))

    @app.post("/v1/swap/{session_id}/broadcast")
    async def broadcast(
        request: Request, session_id: str, payload: BroadcastRequest
    ) -> dict[str, Any]:
        user_id = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=payload.user_id,
        )
        session = await owned_session(session_id, user_id)
        if not _qualified_hash(payload.chain, payload.tx_hash):
            raise HTTPException(
                status_code=422, detail="tx_hash must be a chain-qualified transaction hash"
            )
        if session.broadcast_tx_hash:
            if session.broadcast_tx_hash == payload.tx_hash:
                if session.status not in {
                    "not_propagated",
                    "unknown",
                    "dropped_or_replaced",
                }:
                    return _broadcast_response(session)
            else:
                raise HTTPException(
                    status_code=409, detail="session already has a different broadcast hash"
                )
        provider = app.state.providers.get(session.quote.provider if session.quote else "")
        if provider is None or session.quote is None:
            raise HTTPException(status_code=409, detail="swap provider or quote unavailable")

        broadcast_status: BroadcastStatus = "broadcast_seen"
        try:
            adapter = execution_adapter(payload.chain)
        except Exception:
            adapter = None
        if adapter is not None and _can_observe_broadcast(adapter):
            sender = _display_text(
                session,
                "from",
                "sender",
                "sender_address",
                "owner",
            )
            nonce = _display_int(session, "nonce", "transaction_nonce", "source_nonce")
            chain_status = await _chain_broadcast_status(
                adapter,
                payload.tx_hash,
                sender=sender,
                nonce=nonce,
            )
            if chain_status == "failed":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "TRANSACTION_FAILED",
                        "message": "链上交易已确认失败，请检查钱包和交易参数。",
                        "details": {"tx_hash": payload.tx_hash},
                    },
                )
            # Source-chain confirmation is not swap completion: the order was
            # just registered and the target-chain assets are not delivered yet.
            # Persist source_confirmed (not confirmed) so clients do not render
            # "success" off the source receipt before the order finishes.
            broadcast_status = (
                "source_confirmed" if chain_status == "confirmed" else chain_status
            )
        # Re-check and register under the per-thread lock.  This closes the
        # duplicate-provider-call window when a wallet retries the same hash.
        async with graph_lock(session.thread_id):
            session = await owned_session(session_id, user_id)
            if session.broadcast_tx_hash:
                if session.broadcast_tx_hash == payload.tx_hash:
                    if session.status not in {
                        "not_propagated",
                        "unknown",
                        "dropped_or_replaced",
                    }:
                        return _broadcast_response(session)
                else:
                    raise HTTPException(
                        status_code=409, detail="session already has a different broadcast hash"
                    )
            provider = app.state.providers.get(session.quote.provider if session.quote else "")
            if provider is None or session.quote is None:
                raise HTTPException(status_code=409, detail="swap provider or quote unavailable")
            if broadcast_status not in _BROADCAST_PROVIDER_VISIBLE:
                message = _broadcast_status_messages(session.language).get(
                    broadcast_status,
                    "交易暂时无法登记，请稍后重试。"
                    if session.language != "en"
                    else "The transaction is not ready for provider polling yet.",
                )
                updated = await session_store.update(
                    session_id,
                    expected_revision=session.revision,
                    status=broadcast_status,
                    stage=broadcast_status,
                    broadcast_status=broadcast_status,
                    broadcast_tx_hash=payload.tx_hash,
                    provider_order=None,
                    # The approve is already broadcast at this point; the
                    # unsigned approval transaction is no longer actionable and
                    # must not make the client render a signing card.
                    approval_transaction=None,
                    allowance_requirement=None,
                )
                return _broadcast_response(updated, broadcast_status, message=message)
            reference = session.quote.provider_reference
            if session.pending_transaction is not None:
                transaction_reference = getattr(
                    session.pending_transaction, "provider_reference", None
                )
                if transaction_reference:
                    reference = str(transaction_reference)
            try:
                order = await provider.register_broadcast(
                    reference, payload.tx_hash, quote=session.quote
                )
                order = _provider_order_with_persisted_metadata(
                    order,
                    session.pending_transaction,
                    session.quote,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "PROVIDER_REGISTRATION_FAILED",
                        "message": "兑换服务暂时无法登记交易，请使用同一交易哈希重试。",
                        "details": {
                            "provider": session.quote.provider,
                            "tx_hash": payload.tx_hash,
                            "retryable": True,
                        },
                    },
                ) from exc
            updated = await session_store.update(
                session_id,
                expected_revision=session.revision,
                status="processing",
                stage="processing",
                broadcast_status=broadcast_status,
                broadcast_tx_hash=payload.tx_hash,
                provider_order=order,
                # Once the swap is broadcast, the approval step is complete:
                # drop the stale unsigned approval transaction and allowance
                # requirement so clients stop rendering authorization cards.
                approval_transaction=None,
                allowance_requirement=None,
            )
        return _broadcast_response(updated, broadcast_status)

    @app.post("/v1/swap/{session_id}/select-quote")
    async def select_quote(
        request: Request, session_id: str, payload: QuoteSelectionRequest
    ) -> dict[str, Any]:
        user_id = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=payload.user_id,
        )
        session = await owned_session(session_id, user_id)
        # The app must explicitly choose one of the persisted candidates. A
        # previously selected quote is only valid when its reference matches.
        selected_quote = next(
            (
                candidate
                for candidate in session.quote_candidates
                if candidate.provider_reference == payload.provider_reference
            ),
            None,
        )
        if (
            selected_quote is None
            and session.quote is not None
            and session.quote.provider_reference == payload.provider_reference
        ):
            selected_quote = session.quote
        if (
            selected_quote is None
            or selected_quote.provider_reference != payload.provider_reference
        ):
            raise HTTPException(status_code=404, detail="quote not found")
        if app.state.graph is None:
            return _jsonable(
                await session_store.update(
                    session_id,
                    stage="quote_selected",
                    selected_provider_reference=payload.provider_reference,
                    pending_transaction=None,
                    approval_transaction=None,
                    approval_tx_hash=None,
                    allowance_requirement=None,
                )
            )
        async with graph_lock(session.thread_id):
            result = await app.state.graph.ainvoke(
                {
                    "intent": "swap_select",
                    "forced_intent": "swap_select",
                    "selected_quote": selected_quote.model_dump(mode="json"),
                    # Every explicit selection starts a fresh confirmation
                    # cycle, even when this thread has an interrupted or
                    # previously approved confirmation checkpoint.
                    "confirmation_state": None,
                    "user_confirmation": None,
                    # A new quote invalidates all actionable authorization
                    # state from the previously selected quote in the
                    # checkpoint.  Keep public quote data untouched.
                    "pending_transaction": None,
                    "approval_transaction": None,
                    "approval_tx_hash": None,
                    "allowance_requirement": None,
                    "authorization_stage": None,
                },
                config={"configurable": {"thread_id": session.thread_id}},
            )
            await persist_assistant_history(session.thread_id, result)
        updated = await project_session(session_id, result, status="awaiting_confirmation")
        if updated is not None:
            updated = await session_store.update(
                session_id,
                status="awaiting_confirmation",
                stage="confirmation_required",
                selected_provider_reference=payload.provider_reference,
                # ``project_session`` intentionally treats graph state as a
                # sparse patch.  Explicit reselection clears the old
                # actionable projection without erasing unrelated quote data.
                pending_transaction=None,
                approval_transaction=None,
                approval_tx_hash=None,
                allowance_requirement=None,
                last_error=None,
            )
        return _jsonable(updated or result)

    @app.post("/v1/swap/{session_id}/approve-broadcast")
    async def approve_broadcast(
        request: Request, session_id: str, payload: ApprovalBroadcastRequest
    ) -> dict[str, Any]:
        user_id = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=payload.user_id,
        )
        session = await owned_session(session_id, user_id)
        if not _qualified_hash(payload.chain, payload.approve_tx_hash):
            raise HTTPException(
                status_code=422, detail="approve_tx_hash must be a chain-qualified transaction hash"
            )
        async with graph_lock(session.thread_id):
            session = await owned_session(session_id, user_id)
            if session.approval_tx_hash:
                if session.approval_tx_hash == payload.approve_tx_hash:
                    return _jsonable(session)
                raise HTTPException(
                    status_code=409, detail="session already has a different approval hash"
                )
            return _jsonable(
                await session_store.update(
                    session_id,
                    expected_revision=session.revision,
                    approval_tx_hash=payload.approve_tx_hash,
                    stage="approval_submitted",
                )
            )

    @app.post("/v1/swap/{session_id}/continue")
    async def continue_swap(
        request: Request, session_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        user_id = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=(payload or {}).get("user_id"),
        )
        session = await owned_session(session_id, user_id)
        if session.status == "cancelled" or session.stage == "cancelled":
            return _jsonable(session)
        if app.state.graph is None:
            return _jsonable(session)
        graph = app.state.graph
        config = {"configurable": {"thread_id": session.thread_id}}
        async with graph_lock(session.thread_id):
            snapshot = None
            if hasattr(graph, "aget_state"):
                snapshot = await graph.aget_state(config)
            elif hasattr(graph, "get_state"):
                snapshot = graph.get_state(config)
            if _has_approval_interrupt(snapshot):
                result = await graph.ainvoke(
                    Command(resume={"approve_tx_hash": session.approval_tx_hash}),
                    config=config,
                )
            elif _interrupt_values(snapshot):
                return _jsonable(session)
            else:
                selected_quote = session.quote.model_dump(mode="json") if session.quote else None
                result = await graph.ainvoke(
                    {
                        "intent": "swap_allowance",
                        "forced_intent": "swap_allowance",
                        "selected_quote": selected_quote,
                        "approval_tx_hash": session.approval_tx_hash,
                    },
                    config=config,
                )
            await persist_assistant_history(session.thread_id, result)
        response = result.get("response") or {}
        stage = result.get("authorization_stage") or response.get("stage") or "swap_ready"
        return _jsonable(
            await project_session(session_id, result, status=stage)
        )

    @app.post("/v1/transfer/{session_id}/prepare")
    async def prepare_transfer(
        request: Request, session_id: str, payload: TransferPrepareRequest
    ) -> dict[str, Any]:
        user_id = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=payload.user_id,
        )
        session = await owned_session(session_id, user_id)
        if (
            session.stage in {"prepared", "swap_ready", "transfer_ready"}
            and session.pending_transaction is not None
        ):
            return _jsonable(session)
        if app.state.graph is None:
            raise HTTPException(status_code=503, detail="agent graph is not configured")
        async with graph_lock(session.thread_id):
            result = await app.state.graph.ainvoke(
                {
                    "intent": "transfer",
                    "forced_intent": "transfer",
                    "transfer_request": payload.model_dump(mode="json", exclude={"user_id"}),
                },
                config={"configurable": {"thread_id": session.thread_id}},
            )
        response = result.get("response") or {}
        status = "transfer_ready" if response.get("kind") == "transfer_prepare" else "failed"
        return _jsonable(await project_session(session_id, result, status=status) or result)

    @app.post("/v1/transfer/{session_id}/broadcast")
    async def broadcast_transfer(
        request: Request, session_id: str, payload: BroadcastRequest
    ) -> dict[str, Any]:
        user_id = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=payload.user_id,
        )
        session = await owned_session(session_id, user_id)
        if not _qualified_hash(payload.chain, payload.tx_hash):
            raise HTTPException(
                status_code=422, detail="tx_hash must be a chain-qualified transaction hash"
            )
        if session.quote is not None or session.provider_order is not None:
            raise HTTPException(status_code=409, detail="session is not a transfer session")
        pending = session.pending_transaction
        if pending is None:
            raise HTTPException(status_code=409, detail="transfer transaction is not prepared")
        pending_chain = str(getattr(pending, "chain", ""))
        if pending_chain.upper() != payload.chain.upper():
            raise HTTPException(
                status_code=409, detail="broadcast chain does not match prepared transfer"
            )
        if session.broadcast_tx_hash:
            if session.broadcast_tx_hash == payload.tx_hash:
                return _broadcast_response(session)
            raise HTTPException(
                status_code=409, detail="session already has a different broadcast hash"
            )

        try:
            adapter = execution_adapter(payload.chain)
        except Exception:
            adapter = None
        broadcast_status: BroadcastStatus = "unknown"
        if adapter is not None and _can_observe_broadcast(adapter):
            broadcast_status = await _chain_broadcast_status(
                adapter,
                payload.tx_hash,
                sender=_display_text(session, "from", "sender", "sender_address"),
                nonce=_display_int(session, "nonce", "transaction_nonce", "source_nonce"),
            )
        async with graph_lock(session.thread_id):
            session = await owned_session(session_id, user_id)
            if session.broadcast_tx_hash:
                if session.broadcast_tx_hash == payload.tx_hash:
                    return _broadcast_response(session)
                raise HTTPException(
                    status_code=409,
                    detail="session already has a different broadcast hash",
                )
            updated = await session_store.update(
                session_id,
                expected_revision=session.revision,
                status=broadcast_status,
                stage=broadcast_status,
                broadcast_status=broadcast_status,
                broadcast_tx_hash=payload.tx_hash,
            )
        return _broadcast_response(updated, broadcast_status)

    @app.get("/v1/transactions/{chain}/{tx_hash}")
    async def transaction_status(
        request: Request,
        chain: str,
        tx_hash: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=user_id,
        )
        if not _qualified_hash(chain, tx_hash):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_TRANSACTION_HASH",
                    "message": "tx_hash must be a chain-qualified transaction hash",
                    "details": {"chain": chain},
                },
            )
        explorer = app.state.explorer_provider
        if explorer is not None and hasattr(explorer, "get_transaction_detail"):
            try:
                detail = await explorer.get_transaction_detail(chain, tx_hash)
                status_value = _transaction_status_value(detail.status)
                # Surface the moved amount + symbol up top (token layer first,
                # native coin otherwise), mirroring the SSE transaction_status
                # card so a failed ERC20/TRC20 transfer never renders "0 ETH".
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
                # A token transfer whose explorer response has no token layer
                # (failed, or confirmed but not indexed — e.g. a DAI
                # self-transfer) would otherwise render the native "0 ETH" row.
                # Fall back to the session's intended moved asset so the card
                # shows the real amount/symbol.
                if (
                    amount in (None, "", "0", 0)
                    or not symbol
                    or canonical_symbol(str(symbol))
                    == canonical_symbol(str(native_symbol(chain.upper()) or ""))
                ):
                    fallback = await _session_transfer_asset_fallback(
                        app.state.session_store, str(user_id or ""), chain, tx_hash
                    )
                    if fallback:
                        # Keep the explorer's real native amount for a confirmed
                        # native transfer (it already surfaced a nonzero value);
                        # the draft only stands in when the explorer gave
                        # nothing or the moved asset is a token.
                        if (
                            amount in (None, "", "0", 0)
                            or not symbol
                            or fallback.get("address")
                            or fallback.get("token_address")
                        ):
                            fallback_symbol = str(fallback.get("symbol") or "")
                            if fallback_symbol:
                                symbol = fallback_symbol
                            fallback_amount = fallback.get("amount")
                            if fallback_amount not in (None, ""):
                                amount = str(fallback_amount)
                            token_address = fallback.get("address") or fallback.get(
                                "token_address"
                            )
                            if token_address:
                                logo_url = asset_logo_url(
                                    chain.upper(), str(token_address)
                                )
                            elif fallback.get("logo_url"):
                                logo_url = str(fallback["logo_url"])
                # The explorer surfaced a token layer (amount + symbol) but the
                # provider rarely ships an icon for the transfer. When the moved
                # asset is a token and we still have no logo, derive the icon
                # from the session's intended asset so a confirmed/failed card
                # renders the token's real logo instead of a blank row.
                if (
                    not logo_url
                    and symbol
                    and canonical_symbol(str(symbol))
                    != canonical_symbol(str(native_symbol(chain.upper()) or ""))
                ):
                    logo_fallback = await _session_transfer_asset_fallback(
                        app.state.session_store, str(user_id or ""), chain, tx_hash
                    )
                    if logo_fallback:
                        token_address = logo_fallback.get("address") or logo_fallback.get(
                            "token_address"
                        )
                        if token_address:
                            logo_url = asset_logo_url(chain.upper(), str(token_address))
                        elif logo_fallback.get("logo_url"):
                            logo_url = str(logo_fallback["logo_url"])
                result: dict[str, Any] = {
                    "chain": chain.upper(),
                    "tx_hash": tx_hash,
                    "status": status_value,
                    "message": {
                        "pending": "交易已提交，正在等待链上确认。",
                        "confirmed": "交易已确认。",
                        "failed": "交易执行失败或已回滚。",
                        "unknown": "暂时无法确定交易状态。",
                    }.get(status_value, "暂时无法确定交易状态。"),
                    "source": detail.source or "okx",
                    "failure_reason": detail.failure_reason,
                    "eta": detail.eta,
                    "transaction": _jsonable(detail),
                    # The moved amount + symbol and the chain badge + token icon,
                    # mirroring the SSE transaction_status card.
                    "amount": amount,
                    "symbol": symbol,
                    **asset_logo_fields(
                        chain.upper(),
                        symbol,
                        logo_url,
                    ),
                }
                return result
            except OkxExplorerError as exc:
                raise HTTPException(
                    status_code=(
                        422
                        if exc.code in {"OKX_INVALID_ARGUMENT", "OKX_CHAIN_UNSUPPORTED"}
                        else 502
                    ),
                    detail={"code": exc.code, "message": exc.message},
                ) from exc
        if app.state.explorer_provider is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "OKX_CAPABILITY_UNAVAILABLE",
                    "message": "Transaction explorer unavailable.",
                },
            )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "OKX_CAPABILITY_UNAVAILABLE",
                "message": "Transaction explorer unavailable.",
            },
        )

    @app.get("/v1/prices/token")
    async def token_prices(
        request: Request,
        chain: str,
        symbol: str,
        decimals: int = 18,
        address: str | None = None,
    ) -> dict[str, Any]:
        await authenticated_user(request, verifier=token_verifier, required=require_auth)
        provider = app.state.price_provider
        if provider is None:
            raise HTTPException(status_code=503, detail="price provider unavailable")
        try:
            asset = Asset(chain=chain, symbol=symbol, decimals=decimals, address=address)
            prices = await provider.get_prices([asset])
            return {"prices": [_price_logo_item(item) for item in prices]}
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail={"code": "PRICE_QUERY_FAILED", "message": str(exc)}
            ) from exc

    @app.get("/v1/prices/market")
    async def market_prices(
        request: Request,
        chain: str,
        address: str | None = None,
        symbol: str = "TOKEN",
        decimals: int = 18,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        await authenticated_user(
            request, verifier=token_verifier, required=require_auth, fallback_user_id=user_id
        )
        extras = set(request.query_params) - {"chain", "address", "symbol", "decimals", "user_id"}
        if extras:
            raise HTTPException(
                status_code=422,
                detail={"code": "VALIDATION_ERROR", "message": "Unsupported price parameters."},
            )
        provider = app.state.price_provider
        if provider is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PRICE_PROVIDER_UNAVAILABLE",
                    "message": "Price provider unavailable.",
                },
            )
        try:
            asset = Asset(chain=chain.upper(), symbol=symbol, decimals=decimals, address=address)
            method = getattr(provider, "get_market_details", None)
            if method is None:
                raise PriceProviderError("Detailed market prices are unavailable.")
            details = await method([asset])
            return {
                "asset": _asset_logo_item(asset),
                "details": [_price_logo_item(item) for item in details],
            }
        except PriceProviderError as exc:
            raise HTTPException(
                status_code=422 if exc.code == "OKX_INVALID_ARGUMENT" else 502,
                detail={"code": exc.code, "message": str(exc), "details": {"provider": "okx"}},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "PRICE_QUERY_FAILED", "message": "Price provider unavailable."},
            ) from exc

    @app.get("/v1/prices/history")
    async def historical_prices(
        request: Request,
        chain: str,
        address: str | None = None,
        symbol: str = "TOKEN",
        decimals: int = 18,
        period: str = "1h",
        begin: int | None = None,
        end: int | None = None,
        cursor: str | None = None,
        limit: int = 50,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        await authenticated_user(
            request, verifier=token_verifier, required=require_auth, fallback_user_id=user_id
        )
        extras = set(request.query_params) - {
            "chain", "address", "symbol", "decimals", "period", "begin", "end",
            "cursor", "limit", "user_id",
        }
        if extras or period not in {"1m", "5m", "30m", "1h", "1d"} or not 1 <= limit <= 200:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_PRICE_QUERY",
                    "message": "Invalid historical price parameters.",
                },
            )
        provider = app.state.price_provider
        if provider is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PRICE_PROVIDER_UNAVAILABLE",
                    "message": "Price provider unavailable.",
                },
            )
        try:
            asset = Asset(chain=chain.upper(), symbol=symbol, decimals=decimals, address=address)
            page = await provider.get_historical_prices(
                asset, period=period, begin_ms=begin, end_ms=end, cursor=cursor, limit=limit
            )
            # The page itself carries no asset, so pair it with the resolved
            # identity for the card's header icon.
            return {
                **_jsonable(page),
                "asset": _asset_logo_item(asset),
                "chain_logo_url": chain_logo_url(asset.chain),
            }
        except PriceProviderError as exc:
            raise HTTPException(
                status_code=422 if exc.code == "OKX_INVALID_ARGUMENT" else 502,
                detail={"code": exc.code, "message": str(exc), "details": {"provider": "okx"}},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "PRICE_QUERY_FAILED", "message": "Price provider unavailable."},
            ) from exc

    @app.get("/v1/prices/candles")
    async def price_candles(
        request: Request,
        chain: str,
        address: str | None = None,
        symbol: str = "TOKEN",
        decimals: int = 18,
        bar: str = "1H",
        before: int | None = None,
        after: int | None = None,
        limit: int = 100,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        await authenticated_user(
            request, verifier=token_verifier, required=require_auth, fallback_user_id=user_id
        )
        extras = set(request.query_params) - {
            "chain", "address", "symbol", "decimals", "bar", "before", "after", "limit", "user_id",
        }
        if extras or not 1 <= limit <= 299 or not bar.strip():
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_PRICE_QUERY", "message": "Invalid candle parameters."},
            )
        provider = app.state.price_provider
        if provider is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PRICE_PROVIDER_UNAVAILABLE",
                    "message": "Price provider unavailable.",
                },
            )
        try:
            asset = Asset(chain=chain.upper(), symbol=symbol, decimals=decimals, address=address)
            candles = await provider.get_candles(
                asset, bar=bar, before_ms=before, after_ms=after, limit=limit
            )
            return {
                "candles": [_jsonable(item) for item in candles],
                "asset": _asset_logo_item(asset),
                "chain_logo_url": chain_logo_url(asset.chain),
            }
        except PriceProviderError as exc:
            raise HTTPException(
                status_code=422 if exc.code == "OKX_INVALID_ARGUMENT" else 502,
                detail={"code": exc.code, "message": str(exc), "details": {"provider": "okx"}},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "PRICE_QUERY_FAILED", "message": "Price provider unavailable."},
            ) from exc

    @app.get("/v1/swap/{session_id}")
    async def get_swap(
        request: Request, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        if is_admin_token(request, admin_token):
            session = await session_store.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="swap session not found")
        else:
            authenticated = await authenticated_user(
                request,
                verifier=token_verifier,
                required=require_auth,
                fallback_user_id=user_id,
            )
            session = await owned_session(session_id, authenticated)
        # The App polls this endpoint passively while a swap is in flight. Auto-
        # refresh a live provider order so completion (order_status=completed,
        # broadcast_status=confirmed) surfaces without waiting for a chat turn.
        session = await _poll_swap_order_status(
            session,
            app.state.providers,
            session_store,
            execution_adapter=execution_adapter,
        )
        return _jsonable(session)

    async def wallet_operation(address: str, chain: str, operation: str, limit: int = 20) -> Any:
        wallet_provider = app.state.wallet_provider
        if operation == "balances" and wallet_provider is not None and hasattr(
            wallet_provider, "get_token_balances"
        ):
            try:
                indexes = _okx_chain_indexes(chain)
                balances = await wallet_provider.get_token_balances(address, indexes)
                native = [item for item in balances if item.asset.address is None]
                tokens = [item for item in balances if item.asset.address is not None]
                return {
                    "native": _balance_logo_item(native[0]) if native else None,
                    "tokens": [_balance_logo_item(item) for item in tokens],
                    "source": "okx",
                }
            except OkxWalletError as exc:
                raise HTTPException(
                    status_code=422
                    if exc.code in {"OKX_CHAIN_UNSUPPORTED", "OKX_INVALID_ARGUMENT"}
                    else 502,
                    detail={"code": exc.code, "message": exc.message, "details": exc.details},
                ) from exc
        explorer = app.state.explorer_provider
        if operation == "transactions" and explorer is not None and hasattr(
            explorer, "get_transaction_history"
        ):
            try:
                page = await explorer.get_transaction_history(address, chain, limit=limit)
                return {
                    "source": "okx",
                    "history_kind": "full",
                    "cursor": page.next_cursor,
                    "next_cursor": page.next_cursor,
                    "transactions": [_history_logo_item(item) for item in page.transactions],
                }
            except OkxExplorerError as exc:
                raise HTTPException(
                    status_code=422
                    if exc.code in {"OKX_CHAIN_UNSUPPORTED", "OKX_INVALID_ARGUMENT"}
                    else 502,
                    detail={"code": exc.code, "message": exc.message},
                ) from exc
        if operation in {"balances", "transactions"}:
            if operation == "balances":
                try:
                    adapter = execution_adapter(chain)
                except Exception:
                    adapter = None
                if adapter is not None and hasattr(adapter, "get_native_balance"):
                    try:
                        native = await adapter.get_native_balance(address)
                        tokens = (
                            await adapter.get_token_balances(address)
                            if hasattr(adapter, "get_token_balances")
                            else []
                        )
                    except Exception as exc:
                        raise HTTPException(
                            status_code=502,
                            detail={
                                "code": "BALANCE_QUERY_FAILED",
                                "message": str(exc),
                            },
                        ) from exc
                    return {
                        "native": _balance_logo_item(native),
                        "tokens": [_balance_logo_item(item) for item in tokens],
                        "source": "chain",
                    }
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "OKX_CAPABILITY_UNAVAILABLE",
                    "message": "Wallet data provider unavailable.",
                },
            )
        try:
            adapter = execution_adapter(chain)
            fee = _jsonable(await adapter.estimate_fee())
            # The fee is paid in the chain's native coin; say which chain so the
            # App can show the badge without re-deriving it from the request.
            return {
                **(fee if isinstance(fee, Mapping) else {"fee_estimate": fee}),
                "chain": chain.upper(),
                **asset_logo_fields(chain.upper(), native_symbol(chain)),
            }
        except ChainCapabilityUnavailable as exc:
            raise HTTPException(
                status_code=422,
                detail=exc.to_agent_error().model_dump(mode="json"),
            ) from exc

    @app.get("/v1/wallet/{address}/balances")
    async def balances(request: Request, address: str, chain: str) -> Any:
        await authenticated_user(request, verifier=token_verifier, required=require_auth)
        return await wallet_operation(address, chain, "balances")

    @app.get("/v1/wallet/{address}/transactions")
    async def transactions(request: Request, address: str, chain: str, limit: int = 20) -> Any:
        await authenticated_user(request, verifier=token_verifier, required=require_auth)
        return await wallet_operation(address, chain, "transactions", limit)

    @app.get("/v1/wallet/{address}/fees")
    async def fees(request: Request, address: str, chain: str) -> Any:
        await authenticated_user(request, verifier=token_verifier, required=require_auth)
        return await wallet_operation(address, chain, "fees")

    @app.get("/v1/wallet/{address}/portfolio")
    async def portfolio(request: Request, address: str, chain: str) -> Any:
        await authenticated_user(request, verifier=token_verifier, required=require_auth)
        wallet_provider = app.state.wallet_provider
        if wallet_provider is not None and hasattr(wallet_provider, "get_token_balances"):
            try:
                indexes = _okx_chain_indexes(chain)
                balances = await wallet_provider.get_token_balances(address, indexes)
                prices = []
                if app.state.price_provider is not None:
                    try:
                        prices = await app.state.price_provider.get_prices(
                            [balance.asset for balance in balances]
                        )
                    except Exception:
                        prices = []
                price_map = {
                    (
                        str(price.asset.chain).upper(),
                        str(price.asset.symbol).upper(),
                        str(price.asset.address or "").lower(),
                    ): price
                    for price in prices
                }
                assets = []
                total_usd = 0
                has_value = False
                for balance in balances:
                    # Same row shape as the SSE portfolio card: chain badge plus
                    # the asset icon (native coins reuse the chain logo).
                    item = _balance_logo_item(balance)
                    key = (
                        str(balance.asset.chain).upper(),
                        str(balance.asset.symbol).upper(),
                        str(balance.asset.address or "").lower(),
                    )
                    price = price_map.get(key)
                    if price is not None:
                        usd_value = balance.amount * price.usd_price
                        item["usd_value"] = str(usd_value)
                        item["unit_price"] = str(price.usd_price)
                        total_usd += usd_value
                        has_value = True
                    assets.append(item)
                return {
                    "address": address,
                    "chain": chain.upper(),
                    "assets": assets,
                    "total_usd_value": str(total_usd) if has_value else None,
                    "has_assets": bool(assets),
                    "price_status": "available" if prices else "unavailable",
                    "price_snapshots": [_price_logo_item(price) for price in prices],
                    "source": "okx",
                }
            except OkxWalletError as exc:
                raise HTTPException(
                    status_code=422
                    if exc.code in {"OKX_CHAIN_UNSUPPORTED", "OKX_INVALID_ARGUMENT"}
                    else 502,
                    detail={"code": exc.code, "message": exc.message, "details": exc.details},
                ) from exc
        raise HTTPException(
            status_code=503,
            detail={
                "code": "OKX_CAPABILITY_UNAVAILABLE",
                "message": "Wallet data provider unavailable.",
            },
        )

    @app.get("/v1/wallet/{address}/total-value")
    async def total_value(
        request: Request,
        address: str,
        chains: str,
        asset_type: str = "all",
        exclude_risk_tokens: bool = True,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        await authenticated_user(
            request, verifier=token_verifier, required=require_auth, fallback_user_id=user_id
        )
        provider = app.state.wallet_provider
        if provider is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "WALLET_PROVIDER_UNAVAILABLE",
                    "message": "Wallet provider unavailable.",
                },
            )
        try:
            indexes = _okx_chain_indexes(chains)
            type_value = {"all": "0", "token": "1", "defi": "2"}.get(asset_type.lower())
            if type_value is None:
                raise OkxWalletError(
                    "OKX_INVALID_ARGUMENT",
                    "Unsupported asset_type.",
                    details={"asset_type": asset_type},
                )
            value = await provider.get_total_value(
                address,
                indexes,
                asset_type=type_value,
                exclude_risk_tokens=exclude_risk_tokens,
            )
            return {
                **_jsonable(value),
                "source": "okx",
                "observed_at": _jsonable(value.observed_at),
                # Name the requested chains so the App can badge the total
                # without mapping OKX chain indexes itself.
                "chains": [item.upper() for item in str(chains).split(",") if item.strip()],
                "chain_logo_urls": {
                    item.upper(): chain_logo_url(item)
                    for item in str(chains).split(",")
                    if item.strip()
                },
            }
        except OkxWalletError as exc:
            raise HTTPException(
                status_code=(
                    422
                    if exc.code in {"OKX_CHAIN_UNSUPPORTED", "OKX_INVALID_ARGUMENT"}
                    else 502
                ),
                detail={"code": exc.code, "message": exc.message, "details": exc.details},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "OKX_WALLET_UNAVAILABLE",
                    "message": "Wallet provider unavailable.",
                },
            ) from exc

    @app.get("/v1/wallet/{address}/gas")
    async def gas(
        request: Request,
        address: str,
        chain: str,
        to: str | None = None,
        data: str | None = None,
    ) -> Any:
        await authenticated_user(request, verifier=token_verifier, required=require_auth)
        wallet_provider = app.state.wallet_provider
        if wallet_provider is None or not hasattr(wallet_provider, "get_token_balances"):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "OKX_CAPABILITY_UNAVAILABLE",
                    "message": "Wallet data provider unavailable.",
                },
            )
        try:
            adapter = execution_adapter(chain)
            try:
                fee = await adapter.estimate_fee(to=to, data=data, from_address=address)
            except TypeError:
                fee = await adapter.estimate_fee(to=to, data=data)
            balances = await wallet_provider.get_token_balances(
                address, _okx_chain_indexes(chain)
            )
            native = next((item for item in balances if item.asset.address is None), None)
            if native is None:
                raise OkxWalletError(
                    "OKX_MALFORMED_RESPONSE", "Native balance is unavailable."
                )
            fee_raw = int(fee.amount_raw)
            balance_raw = int(native.amount_raw)
            sufficient = balance_raw >= fee_raw
            return {
                "address": address,
                "chain": chain.upper(),
                "fee_estimate": _jsonable(fee),
                "native_balance": _balance_logo_item(native),
                "sufficient": sufficient,
                "shortfall_raw": str(max(fee_raw - balance_raw, 0)),
                "message": (
                    "当前原生币余额足够支付预计网络手续费。"
                    if sufficient
                    else "当前原生币余额不足以支付预计网络手续费。"
                ),
                # The gas row is about the native coin, whose icon is the chain
                # logo.
                **asset_logo_fields(chain.upper(), native.asset.symbol),
            }
        except ChainCapabilityUnavailable as exc:
            raise HTTPException(
                status_code=422,
                detail=exc.to_agent_error().model_dump(mode="json"),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "GAS_CHECK_FAILED", "message": str(exc)},
            ) from exc

    @app.get("/v1/assets")
    async def assets(
        request: Request,
        chain: str | None = None,
        search: str | None = None,
        provider: str | None = None,
    ) -> Any:
        await authenticated_user(request, verifier=token_verifier, required=require_auth)
        providers_to_query = app.state.providers
        if provider:
            providers_to_query = {
                name: item for name, item in providers_to_query.items() if name == provider
            }
        if not providers_to_query:
            raise HTTPException(status_code=503, detail="asset provider unavailable")
        from wallet_agent.domain.models import AssetQuery

        query = AssetQuery(chain=chain, search=search, provider=provider)
        result: dict[tuple[str, str, str], Any] = {}
        errors = []
        for provider_name, asset_provider in providers_to_query.items():
            if not hasattr(asset_provider, "list_assets"):
                continue
            try:
                values = await asset_provider.list_assets(query)
                for asset in values:
                    key = (
                        str(asset.chain).upper(),
                        str(asset.symbol).upper(),
                        str(asset.address or "").lower(),
                    )
                    result[key] = asset
            except Exception as exc:
                errors.append(
                    {
                        "provider": provider_name,
                        "code": "ASSET_DISCOVERY_FAILED",
                        "message": str(exc),
                    }
                )
        return {
            "query": _jsonable(query),
            "assets": [_asset_logo_item(asset) for asset in result.values()],
            "provider_errors": errors,
        }

    return app


app = create_app()
