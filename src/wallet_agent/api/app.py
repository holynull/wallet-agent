"""REST and SSE endpoints for embedding the agent in a wallet app."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, model_validator

from wallet_agent.chains._common import receipt_success
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
from wallet_agent.persistence import (
    InMemorySessionStore,
    SessionRevisionConflict,
    SessionStore,
    SwapSessionRecord,
)

from .auth import TokenVerifier
from .dependencies import authenticated_user

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


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    message: str
    address: str | None = None
    chain: str | None = None
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


async def _chain_broadcast_status(adapter: Any, tx_hash: str) -> str:
    """Classify a wallet-returned hash without treating RPC propagation as failure."""
    last_error: Exception | None = None
    receipt_lookup_succeeded = False
    for attempt in range(_BROADCAST_RPC_ATTEMPTS):
        try:
            receipt = await adapter.get_transaction_receipt(tx_hash)
            receipt_lookup_succeeded = True
            if receipt is not None:
                success = receipt_success(receipt)
                if success is False:
                    return "failed"
                return "broadcasted" if success is True else "broadcast_pending"
            if hasattr(adapter, "get_transaction"):
                transaction = await adapter.get_transaction(tx_hash)
                if transaction is not None:
                    return "broadcast_pending"
        except Exception as exc:
            last_error = exc
        if attempt + 1 < _BROADCAST_RPC_ATTEMPTS:
            await asyncio.sleep(_BROADCAST_RPC_DELAY_SECONDS)
    if last_error is not None and not receipt_lookup_succeeded:
        raise last_error
    return "broadcast_pending"


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
    if chain in {"SOLANA", "SOL"}:
        return 32 <= len(value) <= 128 and all(c.isalnum() for c in value)
    return False


def _looks_like_swap(message: str) -> bool:
    text = message.lower()
    return any(
        token in text
        for token in ("swap", "exchange", "transfer", "send", "兑换", "换成", "换", "转账", "转")
    )


def _interrupt_values(snapshot: Any) -> list[Any]:
    if not getattr(snapshot, "next", ()):
        return []
    values: list[Any] = []
    for task in getattr(snapshot, "tasks", ()):
        for interruption in getattr(task, "interrupts", ()):
            values.append(getattr(interruption, "value", interruption))
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
    price_provider: Any | None = None,
    store: SessionStore | None = None,
    token_verifier: TokenVerifier | None = None,
    require_auth: bool = False,
    model_registry: Any = None,
) -> FastAPI:
    app = FastAPI(title="Wallet Agent", version="0.1.0")
    session_store = store or InMemorySessionStore()
    provider_map = dict(providers or {})
    app.state.graph = graph
    app.state.chain_registry = chain_registry
    app.state.providers = provider_map
    app.state.price_provider = price_provider
    app.state.session_store = session_store
    app.state.runs: dict[str, dict[str, Any]] = {}
    app.state.graph_locks: dict[str, asyncio.Lock] = {}
    app.state.token_verifier = token_verifier
    app.state.require_auth = require_auth
    app.state.model_registry = model_registry

    def graph_lock(thread_id: str) -> asyncio.Lock:
        lock = app.state.graph_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            app.state.graph_locks[thread_id] = lock
        return lock

    demo_dir = Path(__file__).resolve().parents[3] / "demo"
    if demo_dir.is_dir():
        app.mount("/demo", StaticFiles(directory=demo_dir, html=True), name="demo")

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
        return {"status": "ready"}

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
        selected = state.get("selected_quote")
        if selected:
            changes["quote"] = (
                selected
                if isinstance(selected, NormalizedQuote)
                else NormalizedQuote.model_validate(selected)
            )
        candidates = state.get("quote_candidates") or []
        if candidates:
            changes["quote_candidates"] = [
                item if isinstance(item, NormalizedQuote) else NormalizedQuote.model_validate(item)
                for item in candidates
            ]
        pending = state.get("pending_transaction")
        if pending:
            if isinstance(pending, (UnsignedTransaction, DepositOrder)):
                changes["pending_transaction"] = pending
            else:
                try:
                    changes["pending_transaction"] = UnsignedTransaction.model_validate(pending)
                except Exception:
                    changes["pending_transaction"] = DepositOrder.model_validate(pending)
        orders = state.get("provider_orders") or {}
        if orders:
            raw_order = next(iter(orders.values()))
            changes["provider_order"] = (
                raw_order
                if isinstance(raw_order, ProviderOrder)
                else ProviderOrder.model_validate(raw_order)
            )
        snapshot = state.get("status_snapshot")
        if snapshot:
            changes["order_status"] = (
                snapshot
                if isinstance(snapshot, NormalizedOrderStatus)
                else NormalizedOrderStatus.model_validate(snapshot)
            )
        tx_hash = state.get("broadcast_tx_hash")
        if tx_hash:
            changes["broadcast_tx_hash"] = str(tx_hash)
        if state.get("authorization_stage") is not None:
            changes["stage"] = state["authorization_stage"]
        elif state.get("task_stage") is not None:
            changes["stage"] = state["task_stage"]
        response = state.get("response") or {}
        if response.get("kind") == "swap_status":
            raw_status = response.get("status")
            order_state = (
                raw_status.get("status") if isinstance(raw_status, Mapping) else raw_status
            )
            if order_state:
                changes["stage"] = str(order_state)
            if state.get("provider_orders") or state.get("broadcast_tx_hash"):
                # Once a swap has been broadcast, old checkpoint values are no
                # longer actionable and must not make clients render signing cards.
                changes["approval_transaction"] = None
                changes["pending_transaction"] = None
        for field in ("approval_transaction", "approval_tx_hash", "allowance_requirement"):
            if state.get(field) is not None:
                changes[field] = state[field]
        if state.get("preflight") is not None:
            changes["preflight"] = state["preflight"]
        if state.get("confirmation_state") is not None:
            changes["confirmation_state"] = _jsonable(state["confirmation_state"])
        if state.get("swap_gas_estimate") is not None:
            changes["gas_estimate"] = _jsonable(state["swap_gas_estimate"])
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
        if requirement is None or app.state.chain_registry is None:
            return session
        adapter = app.state.chain_registry.get_adapter(requirement.token.chain)
        tx = adapter.build_erc20_approve(
            token=requirement.token,
            owner=requirement.owner,
            spender=requirement.spender,
            amount_raw=requirement.required_amount_raw,
        )
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
        run = app.state.runs.setdefault(run_id, {"events": []})
        run["status"] = "running"
        run.setdefault("events", [])
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        lock = graph_lock(thread_id)
        await lock.acquire()
        try:
            if app.state.graph is None:
                result = input_state
                app.state.runs[run_id]["events"].append({"event": "complete", "state": result})
            else:

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
                        graph_input = _resolve_graph_input(snapshot, graph_input)
                    async for event in app.state.graph.astream(
                        graph_input, config=config, stream_mode="updates"
                    ):
                        app.state.runs[run_id]["events"].append(
                            {"event": "update", "data": event}
                        )
                    snapshot = await get_snapshot()
                    return dict(snapshot.values)

                result = await execute(input_state)
                # A swap turn owns a business session. Once quotes are available,
                # continue the same thread into the confirmation interrupt so the
                # mobile client has one stable session_id for the full lifecycle.
                if (
                    session_id
                    and result.get("response", {}).get("kind") == "swap_quote"
                    and result.get("selected_quote")
                    and not result.get("pending_transaction")
                ):
                    await project_session(session_id, result, status="quoted", run_id=run_id)
                    result = await execute(
                        {
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
                        }
                    )
                snapshot = await get_snapshot()
                result = snapshot.values
                interrupted = bool(getattr(snapshot, "tasks", ())) and bool(
                    getattr(snapshot, "next", ())
                )
                if interrupted or "__interrupt__" in result:
                    app.state.runs[run_id]["status"] = "awaiting_confirmation"
                    app.state.runs[run_id]["events"].append(
                        {"event": "action_required", "state": result}
                    )
                    await project_session(
                        session_id, result, status="awaiting_confirmation", run_id=run_id
                    )
                else:
                    app.state.runs[run_id]["events"].append({"event": "complete", "state": result})
                    app.state.runs[run_id]["status"] = "complete"
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
                    if response_kind == "error" and (
                        result.get("intent") == "swap_quote"
                        or (result.get("active_task") or {}).get("kind") == "swap"
                    ):
                        session_status = "quote_failed"
                    elif response_kind == "swap_quote" and not result.get("selected_quote"):
                        session_status = "quoted"
                    elif response_kind == "clarification":
                        active_task = result.get("active_task") or {}
                        task_stage = result.get("task_stage") or active_task.get("stage")
                        if task_stage == "collecting_parameters":
                            session_status = "collecting_parameters"
                    await project_session(session_id, result, status=session_status, run_id=run_id)
            if app.state.runs[run_id]["status"] == "running":
                app.state.runs[run_id]["status"] = "complete"
        except _GraphInterruptConflict as exc:
            app.state.runs[run_id]["status"] = "failed"
            error = AgentError(
                code=exc.code,
                message=str(exc),
                details={
                    "interrupt_kinds": sorted(kind or "unknown" for kind in exc.kinds)
                },
            ).model_dump(mode="json")
            app.state.runs[run_id]["events"].append(
                {
                    "event": "error",
                    "error": error,
                }
            )
        except Exception as exc:  # errors are returned without exception internals
            app.state.runs[run_id]["status"] = "failed"
            error = AgentError(code="AGENT_EXECUTION_ERROR", message=str(exc)).model_dump(
                mode="json"
            )
            app.state.runs[run_id]["events"].append(
                {
                    "event": "error",
                    "error": error,
                }
            )
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
            lock.release()

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
        raw_portfolio_query = payload.metadata.get("portfolio_query") or payload.metadata.get(
            "portfolio"
        )
        raw_gas_query = payload.metadata.get("gas_request") or payload.metadata.get("gas")
        raw_asset_query = payload.metadata.get("asset_query") or payload.metadata.get("assets")
        session_id = payload.session_id
        if session_id is None and (
            raw_swap_request is not None
            or raw_transfer_request is not None
            or _looks_like_swap(payload.message)
        ):
            session_id = str(uuid.uuid4())
        if session_id:
            if existing_session is None:
                await session_store.save(
                    SwapSessionRecord(
                        session_id=session_id,
                        user_id=user_id,
                        thread_id=conversation_id,
                    )
                )
            elif existing_session.thread_id != conversation_id:
                raise HTTPException(status_code=409, detail="session thread cannot be changed")
        wallet_context: dict[str, Any] = {}
        if payload.address:
            wallet_context["address"] = payload.address
        if payload.chain:
            wallet_context["chain"] = payload.chain
        metadata_chain_id = payload.metadata.get("wallet_chain_id")
        if metadata_chain_id is not None:
            wallet_context["chain_id"] = metadata_chain_id
        input_state = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "model_id": selected_model_id,
            "request": payload.model_dump(mode="json"),
            "messages": [{"role": "user", "content": payload.message}],
            # API-forced intents are turn-scoped. Explicitly overwrite any
            # value left in the LangGraph checkpoint by select/continue APIs.
            "forced_intent": None,
        }
        if existing_session is not None:
            if existing_session.quote is not None:
                input_state["selected_quote"] = existing_session.quote.model_dump(mode="json")
            if existing_session.approval_tx_hash is not None:
                input_state["approval_tx_hash"] = existing_session.approval_tx_hash
            if existing_session.approval_transaction is not None:
                input_state["approval_transaction"] = existing_session.approval_transaction
            if existing_session.allowance_requirement is not None:
                input_state["allowance_requirement"] = existing_session.allowance_requirement
            if existing_session.pending_transaction is not None:
                pending_transaction = existing_session.pending_transaction
                input_state["pending_transaction"] = pending_transaction.model_dump(mode="json")
        if existing_session is not None and existing_session.provider_order is not None:
            order = existing_session.provider_order
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
        app.state.runs[run_id] = {"status": "queued", "events": []}
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
        async def events() -> AsyncIterator[str]:
            seen = 0
            while True:
                if await request.is_disconnected():
                    return
                run = app.state.runs.get(run_id)
                if run is None:
                    yield (
                        "event: error\ndata: "
                        '{"code":"RUN_NOT_FOUND","message":"Run not found."}\n\n'
                    )
                    return
                event_list = run["events"]
                while seen < len(event_list):
                    event = event_list[seen]
                    seen += 1
                    kind = event.get("event", "update")
                    data = json.dumps(_jsonable(event), ensure_ascii=True)
                    yield f"event: {kind}\ndata: {data}\n\n"
                if run.get("status") in {"complete", "failed", "awaiting_confirmation"}:
                    return
                await asyncio.sleep(0.05)

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
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
            result = await graph.ainvoke(
                Command(resume={"approved": True}),
                config=config,
            )
            if hasattr(graph, "aget_state"):
                snapshot = await graph.aget_state(config)
            else:
                snapshot = graph.get_state(config)
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
        return _jsonable(updated or result)

    @app.post("/v1/swap/{session_id}/cancel")
    async def cancel(request: Request, session_id: str, payload: ConfirmRequest) -> dict[str, Any]:
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
                return _jsonable(session)
            raise HTTPException(
                status_code=409, detail="session already has a different broadcast hash"
            )
        provider = app.state.providers.get(session.quote.provider if session.quote else "")
        if provider is None or session.quote is None:
            raise HTTPException(status_code=409, detail="swap provider or quote unavailable")

        broadcast_status = "broadcasted"
        registry = app.state.chain_registry
        if registry is not None:
            try:
                adapter = (
                    registry.get_adapter(payload.chain)
                    if hasattr(registry, "get_adapter")
                    else registry.get(payload.chain)
                )
            except Exception:
                adapter = None
            if adapter is not None and hasattr(adapter, "get_transaction_receipt"):
                try:
                    chain_status = await _chain_broadcast_status(adapter, payload.tx_hash)
                    if chain_status == "failed":
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "TRANSACTION_FAILED",
                                "message": "链上交易已确认失败，请检查钱包和交易参数。",
                                "details": {"tx_hash": payload.tx_hash},
                            },
                        )
                    broadcast_status = chain_status
                except HTTPException:
                    raise
                except Exception as exc:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "TRANSACTION_STATUS_UNAVAILABLE",
                            "message": "暂时无法验证交易是否已广播，请稍后重试。",
                            "details": {"error": str(exc)},
                        },
                    ) from exc
        # Re-check and register under the per-thread lock.  This closes the
        # duplicate-provider-call window when a wallet retries the same hash.
        async with graph_lock(session.thread_id):
            session = await owned_session(session_id, user_id)
            if session.broadcast_tx_hash:
                if session.broadcast_tx_hash == payload.tx_hash:
                    return _jsonable(session)
                raise HTTPException(
                    status_code=409, detail="session already has a different broadcast hash"
                )
            provider = app.state.providers.get(session.quote.provider if session.quote else "")
            if provider is None or session.quote is None:
                raise HTTPException(status_code=409, detail="swap provider or quote unavailable")
            reference = session.quote.provider_reference
            if session.pending_transaction is not None:
                transaction_reference = getattr(
                    session.pending_transaction, "provider_reference", None
                )
                if transaction_reference:
                    reference = str(transaction_reference)
            order = await provider.register_broadcast(reference, payload.tx_hash)
            updated = await session_store.update(
                session_id,
                expected_revision=session.revision,
                status=broadcast_status,
                stage=broadcast_status,
                broadcast_tx_hash=payload.tx_hash,
                provider_order=order,
            )
        return _jsonable(updated)

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
        registry = app.state.chain_registry
        if registry is None:
            raise HTTPException(status_code=503, detail="chain registry unavailable")
        try:
            adapter = (
                registry.get_adapter(chain)
                if hasattr(registry, "get_adapter")
                else registry.get(chain)
            )
            status = await adapter.get_transaction_status(tx_hash)
            status_value = getattr(status, "value", str(status))
            messages = {
                "pending": "交易已提交，正在等待链上确认。",
                "confirmed": "交易已确认。",
                "failed": "交易执行失败或已回滚。",
                "dropped": "交易可能已被节点丢弃，请检查钱包或重新提交。",
                "unknown": "暂时无法确定交易状态。",
            }
            result: dict[str, Any] = {
                "chain": chain.upper(),
                "tx_hash": tx_hash,
                "status": status_value,
                "message": messages.get(status_value, messages["unknown"]),
            }
            if hasattr(adapter, "get_transaction_receipt"):
                receipt = await adapter.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    result["receipt"] = _jsonable(receipt)
            return result
        except ChainCapabilityUnavailable as exc:
            raise HTTPException(
                status_code=422,
                detail=exc.to_agent_error().model_dump(mode="json"),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "TRANSACTION_STATUS_FAILED",
                    "message": str(exc),
                    "details": {"chain": chain, "tx_hash": tx_hash},
                },
            ) from exc

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
            return {"prices": [_jsonable(item) for item in prices]}
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail={"code": "PRICE_QUERY_FAILED", "message": str(exc)}
            ) from exc

    @app.get("/v1/swap/{session_id}")
    async def get_swap(
        request: Request, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        authenticated = await authenticated_user(
            request,
            verifier=token_verifier,
            required=require_auth,
            fallback_user_id=user_id,
        )
        return _jsonable(await owned_session(session_id, authenticated))

    async def wallet_operation(address: str, chain: str, operation: str, limit: int = 20) -> Any:
        registry = app.state.chain_registry
        if registry is None:
            raise HTTPException(status_code=503, detail="chain registry unavailable")
        try:
            adapter = (
                registry.get_adapter(chain)
                if hasattr(registry, "get_adapter")
                else registry.get(chain)
            )
            if operation == "balances":
                return {
                    "native": _jsonable(await adapter.get_native_balance(address)),
                    "tokens": _jsonable(await adapter.get_token_balances(address)),
                }
            if operation == "transactions":
                return _jsonable(await adapter.get_transaction_history(address, limit=limit))
            return _jsonable(await adapter.estimate_fee())
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
        registry = app.state.chain_registry
        if registry is None:
            raise HTTPException(status_code=503, detail="chain registry unavailable")
        try:
            adapter = (
                registry.get_adapter(chain)
                if hasattr(registry, "get_adapter")
                else registry.get(chain)
            )
            balances = [await adapter.get_native_balance(address)]
            if hasattr(adapter, "get_token_balances"):
                balances.extend(await adapter.get_token_balances(address))
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
                item = _jsonable(balance)
                key = (
                    str(balance.asset.chain).upper(),
                    str(balance.asset.symbol).upper(),
                    str(balance.asset.address or "").lower(),
                )
                price = price_map.get(key)
                if price is not None:
                    usd_value = balance.amount * price.usd_price
                    item["usd_value"] = str(usd_value)
                    total_usd += usd_value
                    has_value = True
                assets.append(item)
            return {
                "address": address,
                "chain": chain.upper(),
                "assets": assets,
                "total_usd_value": str(total_usd) if has_value else None,
                "price_status": "available" if prices else "unavailable",
                "price_snapshots": [_jsonable(price) for price in prices],
            }
        except ChainCapabilityUnavailable as exc:
            raise HTTPException(
                status_code=422,
                detail=exc.to_agent_error().model_dump(mode="json"),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "PORTFOLIO_QUERY_FAILED", "message": str(exc)},
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
        registry = app.state.chain_registry
        if registry is None:
            raise HTTPException(status_code=503, detail="chain registry unavailable")
        try:
            adapter = (
                registry.get_adapter(chain)
                if hasattr(registry, "get_adapter")
                else registry.get(chain)
            )
            try:
                fee = await adapter.estimate_fee(to=to, data=data, from_address=address)
            except TypeError:
                fee = await adapter.estimate_fee(to=to, data=data)
            native = await adapter.get_native_balance(address)
            fee_raw = int(fee.amount_raw)
            balance_raw = int(native.amount_raw)
            sufficient = balance_raw >= fee_raw
            return {
                "address": address,
                "chain": chain.upper(),
                "fee_estimate": _jsonable(fee),
                "native_balance": _jsonable(native),
                "sufficient": sufficient,
                "shortfall_raw": str(max(fee_raw - balance_raw, 0)),
                "message": (
                    "当前原生币余额足够支付预计网络手续费。"
                    if sufficient
                    else "当前原生币余额不足以支付预计网络手续费。"
                ),
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
            "assets": [_jsonable(asset) for asset in result.values()],
            "provider_errors": errors,
        }

    return app


app = create_app()
