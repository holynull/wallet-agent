import base64
import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.api.request_signature import RedisRequestReplayStore
from wallet_agent.persistence import InMemoryRunStore, InMemorySessionStore, SwapSessionRecord


def request_signature(
    secret: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
    timestamp: str,
    nonce: str,
) -> str:
    canonical = "\n".join(
        (
            method.upper(),
            path,
            query,
            timestamp,
            nonce,
            hashlib.sha256(body).hexdigest(),
        )
    )
    digest = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def signed_headers(
    secret: str,
    method: str,
    path: str,
    *,
    query: str = "",
    body: bytes = b"",
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    timestamp_value = str(timestamp if timestamp is not None else int(time.time() * 1000))
    nonce_value = nonce or uuid.uuid4().hex
    return {
        "X-Timestamp": timestamp_value,
        "X-Nonce": nonce_value,
        "X-Signature": request_signature(
            secret,
            method,
            path,
            query,
            body,
            timestamp_value,
            nonce_value,
        ),
        "X-App-Version": "1.6.9",
        "X-Platform": "ios",
    }


def signature_app(
    *,
    mode: str,
    secret: str | None = "shared-secret",
    keys: dict[str, str] | None = None,
):
    store = InMemorySessionStore()
    return create_app(
        store=store,
        request_signature_mode=mode,
        request_signature_secret=secret,
        request_signature_keys=keys,
    ), store


class FakeRedisSet:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True


@pytest.mark.asyncio
async def test_redis_replay_store_claims_a_nonce_once():
    store = RedisRequestReplayStore(FakeRedisSet(), key_prefix="test")

    assert await store.claim("nonce-1", ttl_seconds=60) is True
    assert await store.claim("nonce-1", ttl_seconds=60) is False
    assert await store.claim("nonce-2", ttl_seconds=60) is True


@pytest.mark.asyncio
async def test_signature_enforce_rejects_missing_signature():
    app, _ = signature_app(mode="enforce")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/agent/conversations?user_id=alice")

    assert response.status_code == 401
    assert response.json()["code"] == "REQUEST_SIGNATURE_REQUIRED"
    assert response.json()["request_id"] == response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_signature_enforce_accepts_signed_query_once_and_rejects_replay():
    app, _ = signature_app(mode="enforce")
    path = "/v1/agent/conversations"
    query = "user_id=alice&limit=10"
    headers = signed_headers("shared-secret", "GET", path, query=query)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        accepted = await client.get(f"{path}?{query}", headers=headers)
        replayed = await client.get(f"{path}?{query}", headers=headers)

    assert accepted.status_code == 200
    assert replayed.status_code == 401
    assert replayed.json()["code"] == "REPLAYED_REQUEST_NONCE"


@pytest.mark.asyncio
async def test_signature_enforce_hashes_the_exact_request_body():
    app, store = signature_app(mode="enforce")
    await store.save(
        SwapSessionRecord(
            session_id="s-signature",
            user_id="alice",
            thread_id="t-signature",
            status="awaiting_confirmation",
        )
    )
    path = "/v1/swap/s-signature/confirm"
    body = json.dumps(
        {"user_id": "alice", "approved": False},
        separators=(",", ":"),
    ).encode()
    headers = signed_headers(
        "shared-secret",
        "POST",
        path,
        body=body,
    ) | {"Content-Type": "application/json"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(path, content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    session = await store.get("s-signature")
    assert session is not None
    assert session.status == "cancelled"


@pytest.mark.asyncio
async def test_signature_enforce_rejects_stale_timestamp():
    app, _ = signature_app(mode="enforce")
    path = "/v1/agent/conversations"
    query = "user_id=alice"
    headers = signed_headers(
        "shared-secret",
        "GET",
        path,
        query=query,
        timestamp=int(time.time() * 1000) - 600_000,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"{path}?{query}", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "STALE_REQUEST_TIMESTAMP"


@pytest.mark.asyncio
async def test_signature_warn_records_invalid_request_without_rejecting():
    app, _ = signature_app(mode="warn")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/agent/conversations?user_id=alice")

    assert response.status_code == 200
    assert response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_signature_off_and_health_are_exempt():
    app, _ = signature_app(mode="off")
    enforced_app, _ = signature_app(mode="enforce")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        off_response = await client.get("/v1/agent/conversations?user_id=alice")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=enforced_app), base_url="http://test"
    ) as client:
        health = await client.get("/health")

    assert off_response.status_code == 200
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_signature_keys_are_selected_by_key_id():
    app, _ = signature_app(
        mode="enforce",
        secret=None,
        keys={"ios-v1": "ios-secret", "android-v1": "android-secret"},
    )
    path = "/v1/agent/conversations"
    query = "user_id=alice"
    ios_headers = signed_headers("ios-secret", "GET", path, query=query) | {
        "X-Key-Id": "ios-v1"
    }
    android_headers = signed_headers("android-secret", "GET", path, query=query) | {
        "X-Key-Id": "android-v1"
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        ios = await client.get(f"{path}?{query}", headers=ios_headers)
        android = await client.get(f"{path}?{query}", headers=android_headers)
        missing_key_id = await client.get(f"{path}?{query}", headers=ios_headers | {"X-Key-Id": ""})
        unknown_key_id = await client.get(
            f"{path}?{query}", headers=ios_headers | {"X-Key-Id": "unknown"}
        )

    assert ios.status_code == 200
    assert android.status_code == 200
    assert missing_key_id.status_code == 401
    assert missing_key_id.json()["code"] == "REQUEST_SIGNATURE_KEY_REQUIRED"
    assert unknown_key_id.status_code == 401
    assert unknown_key_id.json()["code"] == "UNKNOWN_SIGNATURE_KEY"


@pytest.mark.asyncio
async def test_operator_only_v1_debug_routes_are_signature_exempt():
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-operator-debug",
            user_id="alice",
            thread_id="conversation-operator-debug",
        )
    )
    runs = InMemoryRunStore()
    await runs.create(
        "run-operator-debug",
        owner_id="alice",
        metadata={"conversation_id": "conversation-operator-debug"},
    )
    app = create_app(
        store=store,
        run_store=runs,
        admin_token="operator-secret",
        request_signature_mode="enforce",
        request_signature_secret="shared-secret",
    )
    headers = {"Authorization": "Bearer operator-secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        timeline = await client.get(
            "/v1/swap/s-operator-debug/timeline", headers=headers
        )
        conversation_runs = await client.get(
            "/v1/agent/conversations/conversation-operator-debug/runs?limit=5",
            headers=headers,
        )

    assert timeline.status_code == 200
    assert conversation_runs.status_code == 200
