from decimal import Decimal

import httpx
import pytest

from wallet_agent.api import StaticTokenVerifier, create_app
from wallet_agent.domain.models import TokenBalance, WalletTotalValue


class WalletProvider:
    async def get_total_value(self, address, chain_indexes, *, asset_type="0", exclude_risk_tokens=True):
        return WalletTotalValue(
            address=address,
            chain_indexes=chain_indexes,
            asset_type=asset_type,
            exclude_risk_tokens=exclude_risk_tokens,
            total_value=Decimal("123.45"),
        )


@pytest.mark.asyncio
async def test_total_value_uses_okx_provider_and_normalizes_chain_names():
    app = create_app(wallet_provider=WalletProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/wallet/0x1111111111111111111111111111111111111111/total-value",
            params={"chains": "ETH,BSC", "asset_type": "all"},
        )
    assert response.status_code == 200
    assert response.json()["total_value"] == "123.45"
    assert response.json()["source"] == "okx"
    assert response.json()["chain_indexes"] == ["1", "56"]


@pytest.mark.asyncio
async def test_total_value_is_503_when_okx_is_disabled():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/wallet/0x1111111111111111111111111111111111111111/total-value",
            params={"chains": "ETH"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "WALLET_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_total_value_requires_auth_when_configured():
    app = create_app(
        wallet_provider=WalletProvider(),
        token_verifier=StaticTokenVerifier({"secret": "alice"}),
        require_auth=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/wallet/0x1111111111111111111111111111111111111111/total-value",
            params={"chains": "ETH"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_total_value_rejects_unknown_chain_without_provider_call():
    class FailingProvider(WalletProvider):
        async def get_total_value(self, *args, **kwargs):
            raise AssertionError("provider must not receive arbitrary chains")

    app = create_app(wallet_provider=FailingProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/wallet/0x1111111111111111111111111111111111111111/total-value",
            params={"chains": "ETH,/api/v6/private"},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "OKX_CHAIN_UNSUPPORTED"
