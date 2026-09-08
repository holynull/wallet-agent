import httpx
import pytest

from wallet_agent.api import create_app


@pytest.mark.asyncio
async def test_mobile_demo_page_is_served_by_api_app():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/demo/")

    assert response.status_code == 200
    assert "Wallet Agent Mobile Demo" in response.text
    assert "/v1/agent/turn" in response.text
    assert "/v1/agent/stream/" in response.text
    assert "/v1/swap/" in response.text
    assert "private key" in response.text.lower()
