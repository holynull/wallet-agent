import httpx
import pytest

from wallet_agent.chains.transports import FailoverJsonRpcTransport


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
