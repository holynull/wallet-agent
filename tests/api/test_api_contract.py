import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.domain.errors import ChainCapabilityUnavailable


async def client_for(*, graph=None, chain_registry=None):
    app = create_app(graph=graph, chain_registry=chain_registry)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return app, client


@pytest.mark.asyncio
async def test_health_and_readiness_endpoints():
    app, client = await client_for(graph=object())
    async with client:
        health = await client.get("/health")
        ready = await client.get("/ready")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_validation_and_signing_material_errors_use_stable_envelope():
    _app, client = await client_for()
    async with client:
        malformed = await client.post("/v1/agent/turn", json={"message": "hi"})
        secret = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "message": "hi",
                "metadata": {"private_key": "never-send"},
            },
        )
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "VALIDATION_ERROR"
    assert secret.status_code == 422
    assert secret.json()["code"] == "VALIDATION_ERROR"
    assert "detail" not in secret.json()


@pytest.mark.asyncio
async def test_sse_completion_and_missing_run_contract():
    app, client = await client_for()
    async with client:
        turn = await client.post(
            "/v1/agent/turn", json={"user_id": "alice", "message": "hello"}
        )
        await app.state.runs[turn.json()["run_id"]]["task"]
        stream = await client.get(f"/v1/agent/stream/{turn.json()['run_id']}")
        missing = await client.get("/v1/agent/stream/missing-run")
    assert "event: complete" in stream.text
    assert "RUN_NOT_FOUND" in missing.text
    assert "run not found" in missing.text.lower()


@pytest.mark.asyncio
async def test_unsupported_chain_error_is_stable():
    class Registry:
        def get_adapter(self, chain):
            raise ChainCapabilityUnavailable(chain, "balances")

    _app, client = await client_for(chain_registry=Registry())
    async with client:
        response = await client.get("/v1/wallet/address/balances", params={"chain": "SUI"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "CHAIN_CAPABILITY_UNAVAILABLE"
    assert body["details"]["chain"] == "SUI"
