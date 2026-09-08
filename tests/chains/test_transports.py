import httpx
import pytest

from wallet_agent.chains.transports import FailoverJsonRpcTransport
from wallet_agent.providers.http import HttpJsonTransport


@pytest.mark.asyncio
async def test_json_rpc_transport_fails_over_to_second_endpoint():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "primary" in str(request.url):
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": "0x2a"},
            request=request,
        )

    transport = FailoverJsonRpcTransport(
        ["https://primary.invalid", "https://backup.invalid"],
        max_attempts=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await transport.request("eth_blockNumber", [])
    finally:
        await transport.aclose()
    assert result["result"] == "0x2a"
    assert calls == ["https://primary.invalid", "https://backup.invalid"]


@pytest.mark.asyncio
async def test_http_json_transport_get_retries_and_sends_headers():
    attempts = 0
    observed_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        observed_headers.append(request.headers.get("x-cg-pro-api-key"))
        if attempts < 2:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"ethereum": {"usd": 1.0}}, request=request)

    transport = HttpJsonTransport(
        "https://provider.invalid",
        timeout_seconds=1,
        max_attempts=2,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await transport.get(
            "/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"},
            headers={"x-cg-pro-api-key": "secret"},
        )
    finally:
        await transport.aclose()

    assert result == {"ethereum": {"usd": 1.0}}
    assert attempts == 2
    assert observed_headers == ["secret", "secret"]
