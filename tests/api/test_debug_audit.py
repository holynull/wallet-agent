import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.persistence import (
    InMemoryRunStore,
    InMemorySessionStore,
    SwapSessionRecord,
)

SAFE_REQUEST_ENTRY_KEYS = {
    "request_id",
    "timestamp",
    "timestamp_ms",
    "method",
    "path",
    "route",
    "status",
    "error_code",
    "message",
    "session_id",
    "conversation_id",
    "run_id",
    "user_id",
    "approval_tx_hash",
    "broadcast_tx_hash",
    "signature_status",
    "app_version",
    "platform",
    "duration_ms",
}


@pytest.mark.asyncio
async def test_request_audit_is_disabled_without_admin_token():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/debug/requests")

    assert response.status_code == 404
    assert response.json()["code"] == "DEBUG_ENDPOINTS_DISABLED"


@pytest.mark.asyncio
async def test_request_audit_exposes_safe_recent_errors_and_request_id():
    app = create_app(admin_token="operator-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        errored = await client.get(
            "/v1/swap/missing-session?user_id=alice",
            headers={
                "X-Request-ID": "ios-approve-409-probe",
                "X-App-Version": "1.6.9",
                "X-Platform": "ios",
            },
        )
        assert errored.status_code == 404
        assert errored.headers["x-request-id"] == "ios-approve-409-probe"
        assert errored.json()["request_id"] == "ios-approve-409-probe"

        denied = await client.get("/debug/requests")
        assert denied.status_code == 401
        assert denied.json()["code"] == "ADMIN_TOKEN_REQUIRED"

        response = await client.get(
            "/debug/errors",
            headers={"Authorization": "Bearer operator-secret"},
        )

    assert response.status_code == 200
    entries = response.json()["entries"]
    entry = next(
        item
        for item in entries
        if item["request_id"] == "ios-approve-409-probe"
    )
    assert entry["status"] == 404
    assert entry["error_code"] == "HTTP_404"
    assert entry["message"] == "swap session not found"
    assert entry["session_id"] == "missing-session"
    assert entry["user_id"] == "alice"
    assert entry["app_version"] == "1.6.9"
    assert entry["platform"] == "ios"
    assert set(entry) <= SAFE_REQUEST_ENTRY_KEYS


@pytest.mark.asyncio
async def test_swap_timeline_returns_session_and_request_sequence():
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-timeline",
            user_id="alice",
            thread_id="t-timeline",
            status="approval_submitted",
            stage="approval_submitted",
            approval_tx_hash="0x" + "a" * 64,
        )
    )
    app = create_app(store=store, admin_token="operator-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        refreshed = await client.get(
            "/v1/swap/s-timeline?user_id=alice",
            headers={"Authorization": "Bearer operator-secret"},
        )
        timeline = await client.get(
            "/v1/swap/s-timeline/timeline",
            headers={"Authorization": "Bearer operator-secret"},
        )

    assert refreshed.status_code == 200
    assert timeline.status_code == 200
    body = timeline.json()
    assert body["session_id"] == "s-timeline"
    assert body["status"] == "approval_submitted"
    assert body["stage"] == "approval_submitted"
    assert body["approval_tx_hash"] == "0x" + "a" * 64
    assert [event["path"] for event in body["events"]] == [
        "/v1/swap/s-timeline",
    ]


@pytest.mark.asyncio
async def test_conversation_runs_lists_newest_runs_for_operator():
    runs = InMemoryRunStore()
    await runs.create(
        "run-old",
        owner_id="alice",
        metadata={"conversation_id": "conversation-1", "session_id": "session-1"},
    )
    await runs.create(
        "run-new",
        owner_id="alice",
        metadata={"conversation_id": "conversation-1", "session_id": "session-2"},
    )
    await runs.create(
        "run-other",
        owner_id="alice",
        metadata={"conversation_id": "conversation-2", "session_id": "session-3"},
    )
    app = create_app(run_store=runs, admin_token="operator-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/agent/conversations/conversation-1/runs?limit=20",
            headers={"Authorization": "Bearer operator-secret"},
        )
        denied = await client.get("/v1/agent/conversations/conversation-1/runs")

    assert denied.status_code == 401
    assert response.status_code == 200
    assert [item["run_id"] for item in response.json()["runs"]] == [
        "run-new",
        "run-old",
    ]
