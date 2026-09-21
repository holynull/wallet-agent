import base64
import hashlib
import hmac
import logging

import httpx
import pytest

from wallet_agent.okx.client import OkxSignedClient
from wallet_agent.okx.errors import OkxClientError


@pytest.mark.asyncio
async def test_get_signs_sorted_query_and_required_headers(monkeypatch):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"ok": True}]})

    client = OkxSignedClient(
        "https://web3.okx.com",
        api_key="api-key",
        secret_key="secret",
        passphrase="passphrase",
        project_id="project",
        transport=httpx.MockTransport(handler),
        clock=lambda: "2026-09-21T01:02:03.000Z",
        retry_delay_seconds=0,
    )
    try:
        result = await client.request(
            "get",
            "/api/v6/dex/balance/total-value-by-address",
            query={"chainIndex": "1", "address": "0xabc", "assetType": "0"},
        )
    finally:
        await client.aclose()

    request = seen["request"]
    assert result["data"] == [{"ok": True}]
    assert request.url.raw_path.decode() == (
        "/api/v6/dex/balance/total-value-by-address?address=0xabc&assetType=0&chainIndex=1"
    )
    prehash = (
        "2026-09-21T01:02:03.000ZGET"
        "/api/v6/dex/balance/total-value-by-address?address=0xabc&assetType=0&chainIndex=1"
    )
    expected = base64.b64encode(
        hmac.new(b"secret", prehash.encode(), hashlib.sha256).digest()
    ).decode()
    assert request.headers["OK-ACCESS-KEY"] == "api-key"
    assert request.headers["OK-ACCESS-SIGN"] == expected
    assert request.headers["OK-ACCESS-TIMESTAMP"] == "2026-09-21T01:02:03.000Z"
    assert request.headers["OK-ACCESS-PASSPHRASE"] == "passphrase"
    assert request.headers["OK-ACCESS-PROJECT"] == "project"


@pytest.mark.asyncio
async def test_post_signs_and_sends_the_same_compact_json_bytes():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"code": "0", "msg": "", "data": {"id": "1"}})

    client = OkxSignedClient(
        "https://web3.okx.com",
        api_key="api-key",
        secret_key="secret",
        passphrase="passphrase",
        transport=httpx.MockTransport(handler),
        clock=lambda: "2026-09-21T01:02:03.000Z",
        retry_delay_seconds=0,
    )
    try:
        await client.request(
            "POST",
            "/api/v6/dex/pre-transaction/simulate",
            body={"b": 2, "a": "x"},
        )
    finally:
        await client.aclose()

    request = seen["request"]
    body = b'{"a":"x","b":2}'
    assert request.content == body
    prehash = "2026-09-21T01:02:03.000ZPOST/api/v6/dex/pre-transaction/simulate" + body.decode()
    expected = base64.b64encode(
        hmac.new(b"secret", prehash.encode(), hashlib.sha256).digest()
    ).decode()
    assert request.headers["OK-ACCESS-SIGN"] == expected
    assert request.headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_nonzero_okx_code_raises_redacted_error():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "50101",
                "msg": "bad api key secret-value",
                "data": [],
            },
        )

    client = OkxSignedClient(
        "https://web3.okx.com",
        api_key="api-key",
        secret_key="secret-value",
        passphrase="passphrase-value",
        transport=httpx.MockTransport(handler),
        retry_delay_seconds=0,
    )
    try:
        with pytest.raises(OkxClientError) as raised:
            await client.request("GET", "/api/v6/dex/market/price")
    finally:
        await client.aclose()
    assert raised.value.code == "50101"
    assert raised.value.retryable is False
    assert "secret-value" not in str(raised.value)
    assert "passphrase-value" not in repr(raised.value)


@pytest.mark.asyncio
async def test_retries_transport_and_server_failures_but_not_application_errors():
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, headers={"Retry-After": "0"}, json={"error": "upstream"})
        return httpx.Response(200, json={"code": "0", "msg": "", "data": []})

    client = OkxSignedClient(
        "https://web3.okx.com",
        api_key="k",
        secret_key="s",
        passphrase="p",
        max_attempts=3,
        transport=httpx.MockTransport(handler),
        retry_delay_seconds=0,
    )
    try:
        assert (await client.request("GET", "/api/v6/dex/market/price"))["data"] == []
    finally:
        await client.aclose()
    assert attempts == 3


@pytest.mark.asyncio
async def test_http_auth_failure_is_not_retried():
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"msg": "unauthorized"})

    client = OkxSignedClient(
        "https://web3.okx.com",
        api_key="k",
        secret_key="s",
        passphrase="p",
        max_attempts=3,
        transport=httpx.MockTransport(handler),
        retry_delay_seconds=0,
    )
    try:
        with pytest.raises(OkxClientError) as raised:
            await client.request("GET", "/api/v6/dex/market/price")
    finally:
        await client.aclose()
    assert attempts == 1
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_retry_backoff_has_bounded_jitter_and_safe_diagnostics(caplog):
    attempts = 0
    delays = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"secret": "body-secret"})
        return httpx.Response(200, json={"code": "0", "msg": "", "data": []})

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client = OkxSignedClient(
        "https://web3.okx.com",
        api_key="api-key",
        secret_key="secret-value",
        passphrase="passphrase-value",
        retry_delay_seconds=0.1,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        jitter_fn=lambda base: base * 0.1,
    )
    try:
        with caplog.at_level(logging.DEBUG, logger="wallet_agent.okx.client"):
            await client.request(
                "GET",
                "/api/v6/dex/market/price",
                query={"address": "0xprivate", "tokenContractAddress": "body-secret"},
                body={"signed": "body-secret"},
            )
    finally:
        await client.aclose()

    assert attempts == 2
    assert delays == [pytest.approx(0.11)]
    assert "body-secret" not in caplog.text
    assert "0xprivate" not in caplog.text
    assert "secret-value" not in caplog.text
    assert "passphrase-value" not in caplog.text
    assert "method=GET" in caplog.text
    assert "path=/api/v6/dex/market/price" in caplog.text
    assert "status=503" in caplog.text
    assert "attempt=1" in caplog.text
    assert "elapsed_ms=" in caplog.text
