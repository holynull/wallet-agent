"""REST and SSE endpoints for embedding the agent in a wallet app."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from wallet_agent.domain.models import AgentError, AssetQuery, ProviderOrder
from wallet_agent.persistence import InMemorySessionStore, SessionStore, SwapSessionRecord


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = None
    user_id: str
    message: str
    address: str | None = None
    chain: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    approved: bool


class BroadcastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
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
        return value.startswith("0x") and len(value) == 66 and all(c in "0123456789abcdefABCDEF" for c in value[2:])
    if chain in {"TRON", "TRX"}:
        return len(value) == 64 and all(c.isalnum() for c in value)
    if chain in {"SOLANA", "SOL"}:
        return 32 <= len(value) <= 128 and all(c.isalnum() for c in value)
    return False


def create_app(*, graph: Any = None, chain_registry: Any = None, providers: Mapping[str, Any] | None = None, store: SessionStore | None = None) -> FastAPI:
    app = FastAPI(title="Wallet Agent", version="0.1.0")
    session_store = store or InMemorySessionStore()
    provider_map = dict(providers or {})
    app.state.graph = graph
    app.state.chain_registry = chain_registry
    app.state.providers = provider_map
    app.state.session_store = session_store
    app.state.runs: dict[str, dict[str, Any]] = {}

    async def run_graph(run_id: str, input_state: dict[str, Any], config: dict[str, Any]) -> None:
        app.state.runs[run_id] = {"status": "running", "events": []}
        try:
            if app.state.graph is None:
                result = input_state
                app.state.runs[run_id]["events"].append({"event": "complete", "state": result})
            else:
                async for event in app.state.graph.astream(input_state, config=config, stream_mode="updates"):
                    payload = {"event": "update", "data": event}
                    app.state.runs[run_id]["events"].append(payload)
                result = app.state.graph.get_state(config).values if hasattr(app.state.graph, "get_state") else input_state
                app.state.runs[run_id]["events"].append({"event": "complete", "state": result})
            app.state.runs[run_id]["status"] = "complete"
        except Exception as exc:  # errors are returned without exception internals
            app.state.runs[run_id]["status"] = "failed"
            app.state.runs[run_id]["events"].append({"event": "error", "error": AgentError(code="AGENT_EXECUTION_ERROR", message=str(exc)).model_dump(mode="json")})

    @app.post("/v1/agent/turn")
    async def turn(payload: TurnRequest) -> dict[str, Any]:
        conversation_id = payload.conversation_id or str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        input_state = {
            "conversation_id": conversation_id,
            "user_id": payload.user_id,
            "request": payload.model_dump(mode="json"),
            "messages": [{"role": "user", "content": payload.message}],
        }
        config = {"configurable": {"thread_id": conversation_id}}
        app.state.runs[run_id] = {"status": "queued", "events": []}
        task = asyncio.create_task(run_graph(run_id, input_state, config))
        app.state.runs[run_id]["task"] = task
        return {"run_id": run_id, "conversation_id": conversation_id, "status": "running"}

    @app.get("/v1/agent/stream/{run_id}")
    async def stream(run_id: str, request: Request) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            seen = 0
            while True:
                if await request.is_disconnected():
                    return
                run = app.state.runs.get(run_id)
                if run is None:
                    yield "event: error\ndata: {\"code\":\"RUN_NOT_FOUND\"}\n\n"
                    return
                event_list = run["events"]
                while seen < len(event_list):
                    event = event_list[seen]
                    seen += 1
                    kind = event.get("event", "update")
                    yield f"event: {kind}\ndata: {json.dumps(_jsonable(event), ensure_ascii=True)}\n\n"
                if run.get("status") in {"complete", "failed"}:
                    return
                await asyncio.sleep(0.05)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def owned_session(session_id: str, user_id: str) -> SwapSessionRecord:
        session = await session_store.get(session_id)
        if session is None or session.user_id != user_id:
            raise HTTPException(status_code=404, detail="swap session not found")
        return session

    @app.post("/v1/swap/{session_id}/confirm")
    async def confirm(session_id: str, payload: ConfirmRequest) -> dict[str, Any]:
        session = await owned_session(session_id, payload.user_id)
        if not payload.approved:
            return _jsonable(await session_store.update(session_id, status="cancelled"))
        graph = app.state.graph
        if graph is None:
            return _jsonable(await session_store.update(session_id, status="confirmed"))
        config = {"configurable": {"thread_id": session.thread_id}}
        result = await graph.ainvoke({"user_confirmation": {"approved": True}}, config=config)
        return _jsonable(result)

    @app.post("/v1/swap/{session_id}/broadcast")
    async def broadcast(session_id: str, payload: BroadcastRequest) -> dict[str, Any]:
        session = await owned_session(session_id, payload.user_id)
        if not _qualified_hash(payload.chain, payload.tx_hash):
            raise HTTPException(status_code=422, detail="tx_hash must be a chain-qualified transaction hash")
        if session.broadcast_tx_hash:
            if session.broadcast_tx_hash == payload.tx_hash:
                return _jsonable(session)
            raise HTTPException(status_code=409, detail="session already has a different broadcast hash")
        provider = app.state.providers.get(session.quote.provider if session.quote else "")
        if provider is None or session.quote is None:
            raise HTTPException(status_code=409, detail="swap provider or quote unavailable")
        reference = session.quote.provider_reference
        order = await provider.register_broadcast(reference, payload.tx_hash)
        updated = await session_store.update(session_id, status="broadcasted", broadcast_tx_hash=payload.tx_hash, provider_order=order)
        return _jsonable(updated)

    @app.get("/v1/swap/{session_id}")
    async def get_swap(session_id: str, user_id: str) -> dict[str, Any]:
        return _jsonable(await owned_session(session_id, user_id))

    async def wallet_operation(address: str, chain: str, operation: str, limit: int = 20) -> Any:
        registry = app.state.chain_registry
        if registry is None:
            raise HTTPException(status_code=503, detail="chain registry unavailable")
        adapter = registry.get_adapter(chain) if hasattr(registry, "get_adapter") else registry.get(chain)
        if operation == "balances":
            return {"native": _jsonable(await adapter.get_native_balance(address)), "tokens": _jsonable(await adapter.get_token_balances(address))}
        if operation == "transactions":
            return _jsonable(await adapter.get_transaction_history(address, limit=limit))
        return _jsonable(await adapter.estimate_fee())

    @app.get("/v1/wallet/{address}/balances")
    async def balances(address: str, chain: str) -> Any:
        return await wallet_operation(address, chain, "balances")

    @app.get("/v1/wallet/{address}/transactions")
    async def transactions(address: str, chain: str, limit: int = 20) -> Any:
        return await wallet_operation(address, chain, "transactions", limit)

    @app.get("/v1/wallet/{address}/fees")
    async def fees(address: str, chain: str) -> Any:
        return await wallet_operation(address, chain, "fees")

    return app


app = create_app()
