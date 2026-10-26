import httpx
import pytest

from wallet_agent.api import StaticTokenVerifier, create_app
from wallet_agent.persistence import InMemoryRunStore, InMemorySessionStore, SwapSessionRecord


async def _inspection_app(*, auth: bool = True, admin_token: str | None = None):
    runs = InMemoryRunStore()
    sessions = InMemorySessionStore()
    app = create_app(
        run_store=runs,
        store=sessions,
        token_verifier=(
            StaticTokenVerifier({"alice-token": "alice", "bob-token": "bob"})
            if auth
            else None
        ),
        require_auth=auth,
        admin_token="operator-token" if auth else admin_token,
    )
    await sessions.save(
        SwapSessionRecord(
            session_id="session-1",
            user_id="alice",
            thread_id="conversation-1",
            title="转账排查",
            last_run_id="run-1",
        )
    )
    await runs.create(
        "run-1",
        owner_id="alice",
        metadata={"session_id": "session-1", "conversation_id": "conversation-1"},
    )
    await runs.append(
        "run-1",
        {
            "event": "progress",
            "data": {
                "authorization": "Bearer must-not-leak",
                "api_key": "api-key-must-not-leak",
                "pending_transaction": {
                    "data": "raw-transaction-must-not-leak",
                    "display": {"unsigned_tx_hex": "unsigned-hex-must-not-leak"},
                },
            },
        },
    )
    await runs.append(
        "run-1",
        {
            "event": "complete",
            "state": {
                "intent": "transfer",
                "task_stage": "ready_for_prepare",
                "broadcast_tx_hash": "abc123",
                "response": {
                    "kind": "transfer_prepare",
                    "message": "交易已准备好",
                },
                "react_tool_timings": [
                    {
                        "tool": "wallet_balance",
                        "status": "success",
                        "elapsed_ms": 12,
                    }
                ],
                "model_trace": [
                    {
                        "role": "react_agent",
                        "model_id": "deepseek-chat",
                        "elapsed_ms": 20,
                    }
                ],
                "pending_transaction": {
                    "data": "terminal-raw-transaction-must-not-leak",
                    "display": {"unsigned_tx_hex": "terminal-hex-must-not-leak"},
                },
            },
        },
    )
    await runs.set_status("run-1", "complete")
    return app, runs


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_run_inspection_is_owner_only_and_redacts_transaction_bodies():
    app, _runs = await _inspection_app()
    async with _client(app) as client:
        missing = await client.get("/v1/agent/runs/run-1")
        other_user = await client.get(
            "/v1/agent/runs/run-1",
            headers={"Authorization": "Bearer bob-token"},
        )
        owner = await client.get(
            "/v1/agent/runs/run-1",
            headers={"Authorization": "Bearer alice-token"},
        )

    assert missing.status_code == 401
    assert other_user.status_code == 404
    assert owner.status_code == 200
    body = owner.json()
    assert body["owner_id"] == "alice"
    assert body["session_id"] == "session-1"
    assert body["conversation_id"] == "conversation-1"
    assert body["summary"]["intent"] == "transfer"
    assert body["summary"]["response_kind"] == "transfer_prepare"
    assert body["summary"]["tool_timeline"][0]["tool"] == "wallet_balance"
    assert body["session"]["last_run_id"] == "run-1"
    assert body["event_count"] == 2
    for secret in (
        "must-not-leak",
        "api-key-must-not-leak",
        "raw-transaction-must-not-leak",
        "unsigned-hex-must-not-leak",
        "terminal-raw-transaction-must-not-leak",
        "terminal-hex-must-not-leak",
    ):
        assert secret not in owner.text


@pytest.mark.asyncio
async def test_operator_token_can_inspect_another_users_run():
    app, _runs = await _inspection_app()
    async with _client(app) as client:
        response = await client.get(
            "/v1/agent/runs/run-1",
            headers={"Authorization": "Bearer operator-token"},
        )
        invalid = await client.get(
            "/v1/agent/runs/run-1",
            headers={"Authorization": "Bearer wrong-token"},
        )
        conversation = await client.get(
            "/v1/agent/conversations/conversation-1?user_id=alice",
            headers={"Authorization": "Bearer operator-token"},
        )
        session = await client.get(
            "/v1/swap/session-1?user_id=alice",
            headers={"Authorization": "Bearer operator-token"},
        )

    assert response.status_code == 200
    assert response.json()["owner_id"] == "alice"
    assert invalid.status_code == 401
    assert conversation.status_code == 200
    assert conversation.json()["session_id"] == "session-1"
    assert session.status_code == 200
    assert session.json()["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_run_inspection_event_limit_returns_latest_events():
    app, _runs = await _inspection_app()
    async with _client(app) as client:
        response = await client.get(
            "/v1/agent/runs/run-1?event_limit=1",
            headers={"Authorization": "Bearer alice-token"},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["event_count"] == 2
    assert body["returned_event_count"] == 1
    assert body["events_truncated"] is True
    assert body["events"][0]["event"] == "complete"


@pytest.mark.asyncio
async def test_stream_requires_the_same_run_owner():
    app, _runs = await _inspection_app()
    async with _client(app) as client:
        other_user = await client.get(
            "/v1/agent/stream/run-1",
            headers={"Authorization": "Bearer bob-token"},
        )
        owner = await client.get(
            "/v1/agent/stream/run-1",
            headers={"Authorization": "Bearer alice-token"},
        )

    assert other_user.status_code == 404
    assert owner.status_code == 200
    assert "event: complete" in owner.text


@pytest.mark.asyncio
async def test_unauthenticated_local_mode_keeps_inspection_available():
    app, _runs = await _inspection_app(auth=False)
    async with _client(app) as client:
        run = await client.get("/v1/agent/runs/run-1?user_id=alice")
        missing_stream = await client.get("/v1/agent/stream/missing-run")

    assert run.status_code == 200
    assert run.json()["owner_id"] == "alice"
    assert "RUN_NOT_FOUND" in missing_stream.text


@pytest.mark.asyncio
async def test_configured_operator_token_protects_inspection_when_auth_is_off():
    app, _runs = await _inspection_app(auth=False, admin_token="local-operator")
    async with _client(app) as client:
        missing = await client.get("/v1/agent/runs/run-1")
        operator = await client.get(
            "/v1/agent/runs/run-1",
            headers={"Authorization": "Bearer local-operator"},
        )

    assert missing.status_code == 401
    assert operator.status_code == 200
