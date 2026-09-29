import pytest
import httpx

from wallet_agent.api import create_app


@pytest.mark.asyncio
async def test_configured_demo_origin_can_preflight_agent_turn(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://34.228.188.5:3000")
    app = create_app(graph=object())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/v1/agent/turn",
            headers={
                "Origin": "http://34.228.188.5:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://34.228.188.5:3000"
