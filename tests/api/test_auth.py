import httpx
import pytest

from wallet_agent.api import StaticTokenVerifier, create_app
from wallet_agent.persistence import InMemorySessionStore, SwapSessionRecord


async def auth_client():
    store = InMemorySessionStore()
    app = create_app(
        graph=None,
        store=store,
        token_verifier=StaticTokenVerifier({"alice-token": "alice", "bob-token": "bob"}),
        require_auth=True,
    )
    await store.save(SwapSessionRecord(session_id="s1", user_id="alice", thread_id="t1"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client


@pytest.mark.asyncio
async def test_missing_and_invalid_bearer_tokens_are_rejected():
    client = await auth_client()
    async with client:
        missing = await client.post("/v1/agent/turn", json={"message": "hi"})
        invalid = await client.post(
            "/v1/agent/turn",
            headers={"Authorization": "Bearer nope"},
            json={"message": "hi"},
        )
    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.json()["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_authenticated_owner_can_access_but_cross_user_is_denied():
    client = await auth_client()
    async with client:
        owner = await client.get(
            "/v1/swap/s1", headers={"Authorization": "Bearer alice-token"}
        )
        other = await client.get(
            "/v1/swap/s1", headers={"Authorization": "Bearer bob-token"}
        )
    assert owner.status_code == 200
    assert owner.json()["user_id"] == "alice"
    assert other.status_code == 404


@pytest.mark.asyncio
async def test_authenticated_identity_does_not_trust_body_user_id():
    client = await auth_client()
    async with client:
        response = await client.post(
            "/v1/agent/turn",
            headers={"Authorization": "Bearer alice-token"},
            json={"user_id": "mallory", "message": "hello"},
        )
    assert response.status_code == 200
    assert response.json()["conversation_id"]
