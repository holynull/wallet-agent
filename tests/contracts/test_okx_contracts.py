from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.helpers.contracts import redact_fixture, safe_shape
from wallet_agent.domain.models import TransactionContext
from wallet_agent.okx.client import OkxSignedClient
from wallet_agent.okx.errors import OkxClientError
from wallet_agent.okx.wallet import OkxWalletAdapter, OkxWalletError

ADDRESS = "0x" + "1" * 40
TOKEN = "0x" + "a" * 40
FIXTURES = Path("tests/contracts/fixtures/okx")


class FixtureClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    @classmethod
    def from_directory(cls, directory: str | Path, scenario: str | None = None) -> "FixtureClient":
        root = Path(directory)
        responses: dict[str, object] = {}
        for path in root.glob("*.json"):
            payload = json.loads(path.read_text())
            if scenario is not None and path.stem == "empty_and_malformed":
                responses["*"] = payload["scenarios"][scenario]
            elif "response" in payload:
                responses[path.stem] = payload["response"]
        return cls(responses)

    @classmethod
    def scenario(cls, name: str) -> "FixtureClient":
        payload = json.loads((FIXTURES / "empty_and_malformed.json").read_text())
        return cls({"*": payload["scenarios"][name]})

    async def request(
        self, method: str, path: str, *, query: Any = None, body: Any = None
    ) -> dict[str, object]:
        if "all-token-balances" in path:
            key = "balances_missing_decimals"
        elif "all-tokens" in path:
            key = "token_metadata"
        elif "total-value" in path:
            key = "price"
        elif "pre-transaction" in path:
            key = "pretransaction"
        else:
            key = "*"
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": safe_shape(query),
                "body": redact_fixture(body),
            }
        )
        payload = self.responses[key] if key in self.responses else self.responses["*"]
        return payload  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_realistic_missing_decimals_fixture_is_recovered_from_metadata():
    client = FixtureClient.from_directory(FIXTURES)
    balances = await OkxWalletAdapter(client, {"ETH": "1"}).get_token_balances(ADDRESS, ["ETH"])
    assert balances[0].asset.decimals == 6
    assert balances[0].amount_raw == "12500000"
    assert balances[1].asset.decimals == 18
    assert balances[2].asset.symbol == "USDT"
    assert balances[2].asset.address == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert balances[2].asset.decimals == 6
    assert balances[2].amount_raw == "2500000"
    assert client.calls[1]["query"]["types"]["chainIndex"] == "str"
    assert client.calls[1]["query"]["keys"] == ["chainIndex"]


@pytest.mark.asyncio
async def test_empty_and_null_data_are_stable_malformed_errors():
    for scenario in ("empty", "null"):
        client = FixtureClient.scenario(scenario)
        with pytest.raises(OkxWalletError) as raised:
            await OkxWalletAdapter(client, {"ETH": "1"}).get_total_value(ADDRESS, ["ETH"])
        assert raised.value.code == "OKX_MALFORMED_RESPONSE"


@pytest.mark.asyncio
async def test_non_finite_total_value_is_rejected_without_payload_details():
    client = FixtureClient.from_directory(FIXTURES)
    with pytest.raises(OkxWalletError) as raised:
        await OkxWalletAdapter(client, {"ETH": "1"}).get_total_value(ADDRESS, ["ETH"])
    assert raised.value.code == "OKX_MALFORMED_RESPONSE"
    assert "NaN" not in str(raised.value)


@pytest.mark.asyncio
async def test_inconsistent_raw_balance_is_not_defaulted_to_zero():
    client = FixtureClient.scenario("inconsistent")
    with pytest.raises(OkxWalletError) as raised:
        await OkxWalletAdapter(client, {"ETH": "1"}).get_token_balances(ADDRESS, ["ETH"])
    assert raised.value.code == "OKX_MALFORMED_RESPONSE"


@pytest.mark.asyncio
async def test_out_of_range_metadata_decimals_are_rejected():
    client = FixtureClient.scenario("metadata_out_of_range")
    with pytest.raises(OkxWalletError) as raised:
        await OkxWalletAdapter(client, {"ETH": "1"})._token_metadata("1")
    assert raised.value.code == "OKX_MALFORMED_RESPONSE"


@pytest.mark.asyncio
async def test_application_error_is_normalized_at_wallet_boundary():
    client = FixtureClient.scenario("application_error")
    with pytest.raises(OkxWalletError) as raised:
        await OkxWalletAdapter(client, {"ETH": "1"}).get_total_value(ADDRESS, ["ETH"])
    assert raised.value.code == "OKX_MALFORMED_RESPONSE"


@pytest.mark.asyncio
async def test_signed_client_preserves_sanitized_application_error_code():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads((FIXTURES / "empty_and_malformed.json").read_text())
        return httpx.Response(200, json=payload["scenarios"]["application_error"])

    client = OkxSignedClient(
        "https://web3.okx.com",
        api_key="public",
        secret_key="secret",
        passphrase="pass",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OkxClientError) as raised:
            await client.request("GET", "/api/v6/dex/balance/total-value-by-address")
        assert raised.value.code == "51000"
        assert "data" not in str(raised.value)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pretransaction_fixture_preserves_documented_body_shape():
    client = FixtureClient.from_directory(FIXTURES)
    tx = TransactionContext(
        chain="ETH",
        from_address=ADDRESS,
        to_address=ADDRESS,
        native_amount="1",
        calldata="0x",
    )
    result = await OkxWalletAdapter(client, {"ETH": "1"}).estimate_gas_limit(tx)
    assert result.gas_limit == "21000"
    body = client.calls[-1]["body"]
    assert body["chainIndex"] == "1"
    assert body["fromAddress"] == "<redacted-address>"
    assert body["toAddress"] == "<redacted-address>"
    assert body["txAmount"] == "1"
    assert body["extJson"] == {"inputData": "0x"}
