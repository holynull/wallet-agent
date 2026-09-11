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
    assert "eip6963:requestProvider" in response.text
    assert "catwallet" in response.text.lower()
    assert "window.catWallet" in response.text
    assert "window.catwallet" in response.text
    assert "window.ethereum.providers" in response.text
    assert "isCatWallet" in response.text
    assert "providerPriority" in response.text
    assert "Date.now() + 1200" in response.text
    assert "accountsChanged" in response.text
    assert "wallet_switchEthereumChain" in response.text
    assert "当前兑换会话仍然保留" in response.text
    assert "eth_sendTransaction" in response.text
    assert "transfer_prepare" in response.text
    assert "钱包签名并广播转账" in response.text
    assert "/v1/transactions/" in response.text
    assert "交易预检查" in response.text
    assert "钱包余额" in response.text
    assert "function renderWallet" in response.text
    assert "我找到了 ${quotes.length} 个兑换报价" in response.text
    assert "当前 ${wallet.chain || '钱包'} 的原生资产余额是" in response.text
    assert "资产组合" in response.text
    assert "Token 资产发现" in response.text
    assert "后端调试数据" in response.text
    assert "联调检查" in response.text
    assert "debugSmokeButton" in response.text
    assert "测试全部 Agent 功能" in response.text
    assert "runAgentTestsButton" in response.text
    assert "agent_test_intent" in response.text
    assert "不会自动签名" in response.text
    assert "recordDebug" in response.text
    assert "SSE CONNECT" in response.text
    assert "SSE ${event}" in response.text


@pytest.mark.asyncio
async def test_mobile_demo_documents_quote_selection_and_approval_flow():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        html = (await client.get("/demo/")).text

    assert "select-quote" in html
    assert "/cancel" in html
    assert "approve-broadcast" in html
    assert "provider_reference" in html
    assert "/continue" in html
    assert "private key" in html.lower()
