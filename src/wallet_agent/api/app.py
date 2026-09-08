"""REST and SSE endpoints for embedding the agent in a wallet app."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, model_validator

from wallet_agent.domain.errors import ChainCapabilityUnavailable
from wallet_agent.domain.models import (
    AgentError,
    DepositOrder,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    UnsignedTransaction,
)
from wallet_agent.persistence import InMemorySessionStore, SessionStore, SwapSessionRecord

from .auth import TokenVerifier
from .dependencies import authenticated_user


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = None
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


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _qualified_hash(chain: str, tx_hash: str) -> bool:
    value = tx_hash.strip()
    if not value or any(c.isspace() for c in value):
        return False
    chain = chain.upper()
    if chain in {"EVM", "ETH", "BSC", "BASE", "POLYGON", "ARBITRUM", "OPTIMISM"}:
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


def create_app(
    *,
    graph: Any = None,
    chain_registry: Any = None,
    providers: Mapping[str, Any] | None = None,
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
    app.state.session_store = session_store
    app.state.runs: dict[str, dict[str, Any]] = {}
    app.state.token_verifier = token_verifier
    app.state.require_auth = require_auth
    app.state.model_registry = model_registry

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
    ) -> SwapSessionRecord | None:
        """Project only normalized graph values into the app-facing session index."""
        if not session_id:
            return None
        current = await session_store.get(session_id)
        if current is None:
            return None
        changes: dict[str, Any] = {}
        selected = state.get("selected_quote")
        if selected:
            changes["quote"] = (
                selected
                if isinstance(selected, NormalizedQuote)
                else NormalizedQuote.model_validate(selected)
            )
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
        if status:
            changes["status"] = status
        if not changes:
            return current
        return await session_store.update(session_id, **changes)

    async def run_graph(
        run_id: str,
        input_state: dict[str, Any],
        config: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> None:
        app.state.runs[run_id] = {"status": "running", "events": []}
        try:
            if app.state.graph is None:
                result = input_state
                app.state.runs[run_id]["events"].append({"event": "complete", "state": result})
            else:
                async def execute(graph_input: dict[str, Any]) -> dict[str, Any]:
                    async for event in app.state.graph.astream(
                        graph_input, config=config, stream_mode="updates"
                    ):
                        app.state.runs[run_id]["events"].append(
                            {"event": "update", "data": event}
                        )
                    snapshot = app.state.graph.get_state(config)
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
                    await project_session(session_id, result, status="quoted")
                    result = await execute(
                        {
                            "intent": "swap_prepare",
                            "swap_request": result.get("swap_request"),
                            "selected_quote": result.get("selected_quote"),
                            "swap_session": {"session_id": session_id},
                        }
                    )
                result = (
                    app.state.graph.get_state(config).values
                    if hasattr(app.state.graph, "get_state")
                    else result
                )
                snapshot = (
                    app.state.graph.get_state(config)
                    if hasattr(app.state.graph, "get_state")
                    else None
                )
                interrupted = bool(getattr(snapshot, "tasks", ())) and bool(
                    getattr(snapshot, "next", ())
                )
                if interrupted or "__interrupt__" in result:
                    app.state.runs[run_id]["status"] = "awaiting_confirmation"
                    app.state.runs[run_id]["events"].append(
                        {"event": "action_required", "state": result}
                    )
                    await project_session(session_id, result, status="awaiting_confirmation")
                else:
                    app.state.runs[run_id]["events"].append({"event": "complete", "state": result})
                    app.state.runs[run_id]["status"] = "complete"
                    await project_session(session_id, result, status="completed")
            if app.state.runs[run_id]["status"] == "running":
                app.state.runs[run_id]["status"] = "complete"
        except Exception as exc:  # errors are returned without exception internals
            app.state.runs[run_id]["status"] = "failed"
            app.state.runs[run_id]["events"].append(
                {
                    "event": "error",
                    "error": AgentError(code="AGENT_EXECUTION_ERROR", message=str(exc)).model_dump(
                        mode="json"
                    ),
                }
            )

    @app.post("/v1/agent/turn")
    async def turn(request: Request, payload: TurnRequest) -> dict[str, Any]:
        if payload.user_id is None and token_verifier is None and not require_auth:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed.",
                    "details": {
                        "errors": [{"loc": ["body", "user_id"], "msg": "Field required"}]
                    },
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
        conversation_id = payload.conversation_id or str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        raw_swap_request = payload.metadata.get("swap_request") or payload.metadata.get("swap")
        session_id = str(uuid.uuid4()) if raw_swap_request is not None else None
        if session_id:
            await session_store.save(
                SwapSessionRecord(
                    session_id=session_id,
                    user_id=user_id,
                    thread_id=conversation_id,
                )
            )
        input_state = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "model_id": selected_model_id,
            "request": payload.model_dump(mode="json"),
            "messages": [{"role": "user", "content": payload.message}],
        }
        if raw_swap_request is not None:
            input_state["swap_request"] = raw_swap_request
        config = {"configurable": {"thread_id": conversation_id}}
        app.state.runs[run_id] = {"status": "queued", "events": []}
        task = asyncio.create_task(
            run_graph(run_id, input_state, config, session_id=session_id)
        )
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
            return _jsonable(await session_store.update(session_id, status="cancelled"))
        if session.pending_transaction is not None:
            return _jsonable(session)
        graph = app.state.graph
        if graph is None:
            return _jsonable(await session_store.update(session_id, status="confirmed"))
        config = {"configurable": {"thread_id": session.thread_id}}
        result = await graph.ainvoke(
            Command(resume={"approved": True}),
            config=config,
        )
        status = "prepared" if result.get("pending_transaction") else "confirmed"
        updated = await project_session(session_id, result, status=status)
        return _jsonable(updated or result)

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
        reference = session.quote.provider_reference
        order = await provider.register_broadcast(reference, payload.tx_hash)
        updated = await session_store.update(
            session_id,
            status="broadcasted",
            broadcast_tx_hash=payload.tx_hash,
            provider_order=order,
        )
        return _jsonable(updated)

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

    return app


app = create_app()
