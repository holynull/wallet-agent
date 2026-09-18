from decimal import Decimal
from typing import Any

import httpx
import pytest

from wallet_agent.domain.models import Asset, AssetQuery, ProviderOrder, SwapQuoteRequest
from wallet_agent.providers.bridgers import BridgersProvider
from wallet_agent.providers.http import HttpJsonTransport, ProviderResponseError


class FakeTransport:
    def __init__(self, *responses: dict[str, Any]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"path": path, "payload": payload, "idempotency_key": idempotency_key})
        return self.responses.pop(0)


class DynamicQuoteTransport:
    def __init__(self):
        self.calls = []

    async def post(self, path, payload, *, idempotency_key=None):
        self.calls.append(payload)
        raw = int(payload["fromTokenAmount"])
        amount = Decimal(raw) / Decimal(10**6)
        output = amount * Decimal("0.8")
        tx = {
            "toTokenAmount": str(output),
            "amountOutMin": str(int(output * Decimal(10**6))),
            "toTokenDecimal": 6,
            "depositMin": "1",
            "depositMax": "100",
            "fee": "0",
            "chainFee": "0",
        }
        return {"resCode": 100, "resMsg": "success", "data": {"txData": tx}}


@pytest.mark.asyncio
async def test_bridgers_list_assets_normalizes_chain_and_filters_search():
    transport = FakeTransport(
        {
            "resCode": "100",
            "data": {
                "tokens": [
                    {
                        "chain": "ETH",
                        "symbol": "USDC",
                        "decimals": 6,
                        "address": "0x0000000000000000000000000000000000000011",
                    },
                    {
                        "chain": "ETH",
                        "symbol": "USDT(ERC20)",
                        "decimals": 6,
                        "address": "0x0000000000000000000000000000000000000022",
                    },
                ]
            },
        }
    )
    provider = BridgersProvider.from_transport(transport)

    assets = await provider.list_assets(AssetQuery(chain="Ethereum", search="USDT"))

    assert [asset.symbol for asset in assets] == ["USDT(ERC20)"]
    assert transport.calls[0]["payload"] == {"chain": "ETH"}


@pytest.mark.asyncio
async def test_http_transport_bounds_retries_for_transport_failures():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"resCode": 100, "data": {}})

    transport = HttpJsonTransport(
        base_url="https://provider.invalid",
        timeout_seconds=1,
        max_attempts=3,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        response = await transport.post("/quote", {"wallet": "sensitive-address"})
    finally:
        await transport.aclose()

    assert response == {"resCode": 100, "data": {}}
    assert attempts == 3


@pytest.mark.asyncio
async def test_http_transport_does_not_retry_non_transport_http_failures():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, json={"error": "bad request"})

    transport = HttpJsonTransport(
        base_url="https://provider.invalid",
        max_attempts=3,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await transport.post("/quote", {"wallet": "sensitive-address"})
    finally:
        await transport.aclose()

    assert attempts == 1


@pytest.mark.asyncio
async def test_http_transport_retries_retryable_get_statuses():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, json={"error": "rate limited"}, request=request)
        return httpx.Response(200, json={"data": {"ok": True}}, request=request)

    transport = HttpJsonTransport(
        base_url="https://provider.invalid",
        max_attempts=3,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        response = await transport.get("/quote", params={"asset": "ETH"})
    finally:
        await transport.aclose()

    assert response == {"data": {"ok": True}}
    assert attempts == 3


@pytest.mark.asyncio
async def test_http_transport_does_not_retry_permanent_get_statuses():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, json={"error": "not found"}, request=request)

    transport = HttpJsonTransport(
        base_url="https://provider.invalid",
        max_attempts=3,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await transport.get("/quote", params={"asset": "ETH"})
    finally:
        await transport.aclose()

    assert attempts == 1


@pytest.mark.asyncio
async def test_http_transport_sends_idempotency_key_without_logging_payload(caplog):
    observed_key = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_key
        observed_key = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"resCode": 100, "data": {}})

    transport = HttpJsonTransport(
        base_url="https://provider.invalid",
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        await transport.post(
            "/write",
            {"wallet": "sensitive-address"},
            idempotency_key="stable-key",
        )
    finally:
        await transport.aclose()

    assert observed_key == "stable-key"
    assert "sensitive-address" not in caplog.text


def valid_quote_request() -> SwapQuoteRequest:
    return SwapQuoteRequest(
        source_asset=Asset(
            chain="BASE",
            chain_id=8453,
            symbol="USDC",
            decimals=6,
            address="0x0000000000000000000000000000000000000011",
        ),
        destination_asset=Asset(
            chain="BSC",
            chain_id=56,
            symbol="USDT",
            decimals=6,
            address="0x0000000000000000000000000000000000000022",
        ),
        input_amount=Decimal("10"),
        input_amount_raw="10000000",
        sender_address="0x1234567890abcdef1234567890abcdef12345678",
        recipient_address="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        slippage_bps=100,
    )


def quote_response(**overrides: Any) -> dict[str, Any]:
    tx_data = {
        "toTokenAmount": "9.8",
        "amountOutMin": "9700000",
        "fromTokenAmount": "10000000",
        "fromTokenDecimal": 6,
        "toTokenDecimal": 6,
        "depositMin": "0.1",
        "depositMax": "1000",
        "fee": "0.002",
        "chainFee": "0.01",
        "contractAddress": "0x0000000000000000000000000000000000000033",
        "dex": "Bridgers",
        "feeToken": "USDC",
        "path": [],
        "logoUrl": "https://example.invalid/logo.png",
    }
    tx_data.update(overrides)
    return {"resCode": 100, "resMsg": "success", "data": {"txData": tx_data}}


@pytest.mark.asyncio
async def test_bridgers_quote_converts_units_and_preserves_raw_amounts():
    transport = FakeTransport(quote_response())
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")

    quote = await provider.quote(valid_quote_request())

    assert quote.expected_output == Decimal("9.8")
    assert quote.expected_output_raw == "9800000"
    assert quote.minimum_output == Decimal("9.7")
    assert quote.minimum_output_raw == "9700000"
    assert quote.provider_fee == Decimal("0.002")
    assert quote.network_fee == Decimal("0.01")
    assert transport.calls == [
        {
            "path": "/api/sswap/quote",
            "payload": {
                "equipmentNo": "0x1234567890abcdef1234567890abcd",
                "sourceFlag": "wallet-agent",
                "fromTokenAddress": "0x0000000000000000000000000000000000000011",
                "toTokenAddress": "0x0000000000000000000000000000000000000022",
                "fromTokenAmount": "10000000",
                "fromTokenChain": "BASE",
                "toTokenChain": "BSC",
                "userAddr": "0x1234567890abcdef1234567890abcdef12345678",
                "fromCoinCode": "USDC(BASE)",
                "toCoinCode": "USDT(BSC)",
            },
            "idempotency_key": None,
        }
    ]


@pytest.mark.asyncio
async def test_bridgers_reverse_quote_binary_searches_source_amount():
    transport = DynamicQuoteTransport()
    provider = BridgersProvider.from_transport(transport)

    quote = await provider.reverse_quote(valid_quote_request(), Decimal("4"))

    assert quote.input_amount == Decimal("5")
    assert quote.expected_output == Decimal("4")
    assert len(transport.calls) < 40


@pytest.mark.asyncio
async def test_bridgers_evm_quote_exposes_allowance_requirement():
    transport = FakeTransport(quote_response())
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")

    quote = await provider.quote(valid_quote_request())

    assert quote.allowance_requirement is not None
    assert quote.allowance_requirement.spender == "0x0000000000000000000000000000000000000033"
    assert quote.allowance_requirement.required_amount_raw == valid_quote_request().input_amount_raw


@pytest.mark.asyncio
async def test_bridgers_ethereum_erc20_quote_exposes_allowance_requirement():
    transport = FakeTransport(quote_response())
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    request = valid_quote_request().model_copy(
        update={
            "source_asset": valid_quote_request().source_asset.model_copy(
                update={"chain": "ETH", "chain_id": 1}
            ),
            "destination_asset": valid_quote_request().destination_asset.model_copy(
                update={"chain": "ETH", "chain_id": 1}
            ),
        }
    )

    quote = await provider.quote(request)

    assert quote.allowance_requirement is not None
    assert quote.allowance_requirement.spender == "0x0000000000000000000000000000000000000033"


@pytest.mark.asyncio
async def test_bridgers_quote_payload_contains_only_normalized_resume_metadata():
    transport = FakeTransport(
        quote_response(),
        {
            "resCode": "100",
            "resMsg": "success",
            "data": {"txData": {"data": "0xdeadbeef", "to": "0xrouter", "value": "0x0"}},
        },
    )
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")

    quote = await provider.quote(valid_quote_request())

    assert quote.provider_payload == {
        "equipment_no": "0x1234567890abcdef1234567890abcd",
        "source_flag": "wallet-agent",
        "sender_address": "0x1234567890abcdef1234567890abcdef12345678",
        "recipient_address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        "slippage_bps": 100,
        "amount_out_min_raw": "9700000",
    }
    assert "request" not in quote.provider_payload
    assert "tx_data" not in quote.provider_payload

    provider._quotes.clear()
    transaction = await provider.prepare(quote)
    assert transaction.data == "0xdeadbeef"


@pytest.mark.asyncio
async def test_bridgers_prepare_uses_quote_minimum_and_returns_unsigned_transaction():
    transport = FakeTransport(
        quote_response(),
        {
            "resCode": "100",
            "resMsg": "success",
            "data": {
                "txData": {
                    "data": "0xdeadbeef",
                    "to": "0x0000000000000000000000000000000000000033",
                    "value": "0x0",
                }
            },
        },
    )
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    quote = await provider.quote(valid_quote_request())

    transaction = await provider.prepare(quote)

    assert transaction.data == "0xdeadbeef"
    assert transaction.gas_limit is None
    assert transaction.display == {
        "input_amount": "10",
        "expected_output": "9.8",
        "minimum_output": "9.7",
    }
    payload = transport.calls[1]["payload"]
    assert payload["fromTokenAmount"] == "10000000"
    assert payload["amountOutMin"] == "9700000"
    assert payload["slippage"] == "0.01"


@pytest.mark.asyncio
async def test_bridgers_upload_uses_idempotency_key_and_tracks_real_order_id():
    transport = FakeTransport(
        quote_response(),
        {
            "resCode": 100,
            "resMsg": "success",
            "data": {"orderId": "bridgers-order-42"},
        },
    )
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    quote = await provider.quote(valid_quote_request())

    order = await provider.register_broadcast(quote.provider_reference, "0xdeposit")

    assert order.provider_order_id == "bridgers-order-42"
    assert order.tx_hash == "0xdeposit"
    assert transport.calls[1]["idempotency_key"]
    assert transport.calls[1]["payload"]["hash"] == "0xdeposit"


@pytest.mark.asyncio
async def test_bridgers_414_is_idempotent_only_for_upload():
    upload_transport = FakeTransport(
        quote_response(),
        {
            "resCode": 414,
            "resMsg": "already updated",
            "data": {"orderId": "bridgers-order-existing"},
        },
    )
    provider = BridgersProvider.from_transport(upload_transport, source_flag="wallet-agent")
    quote = await provider.quote(valid_quote_request())

    order = await provider.register_broadcast(quote.provider_reference, "0xdeposit")

    assert order.provider_order_id == "bridgers-order-existing"

    quote_provider = BridgersProvider.from_transport(
        FakeTransport({"resCode": 414, "resMsg": "already updated", "data": {}}),
        source_flag="wallet-agent",
    )
    with pytest.raises(ProviderResponseError) as exc_info:
        await quote_provider.quote(valid_quote_request())
    assert exc_info.value.code == "414"
    assert not exc_info.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [412, "413", 415, "777"])
async def test_bridgers_classifies_retryable_response_codes(code: int | str):
    provider = BridgersProvider.from_transport(
        FakeTransport({"resCode": code, "resMsg": "temporary", "data": {}}),
        source_flag="wallet-agent",
    )

    with pytest.raises(ProviderResponseError) as exc_info:
        await provider.quote(valid_quote_request())

    assert exc_info.value.code == str(code)
    assert exc_info.value.retryable
    assert exc_info.value.category == "retryable"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [906, 907, 908, 999, 1114, 1145])
async def test_bridgers_classifies_permanent_response_codes(code: int):
    provider = BridgersProvider.from_transport(
        FakeTransport({"resCode": code, "resMsg": "permanent", "data": {}}),
        source_flag="wallet-agent",
    )

    with pytest.raises(ProviderResponseError) as exc_info:
        await provider.quote(valid_quote_request())

    assert not exc_info.value.retryable
    assert exc_info.value.category == "permanent"


@pytest.mark.asyncio
async def test_bridgers_upload_rejects_success_without_real_order_id():
    provider = BridgersProvider.from_transport(
        FakeTransport(quote_response(), {"resCode": 100, "resMsg": "success", "data": {}}),
        source_flag="wallet-agent",
    )
    quote = await provider.quote(valid_quote_request())

    with pytest.raises(ProviderResponseError, match="orderId"):
        await provider.register_broadcast(quote.provider_reference, "0xdeposit")


@pytest.mark.asyncio
async def test_bridgers_status_queries_real_order_id_and_normalizes_terminal_details():
    transport = FakeTransport(
        {
            "resCode": 100,
            "resMsg": "success",
            "data": {
                "orderId": "bridgers-order-42",
                "status": "refund_complete",
                "hash": "0xdeposit",
                "refundCoinAmt": "9.9",
                "refundHash": "0xrefund",
                "refundHashExplore": "https://example.invalid/refund",
                "refundReason": "slippage",
            },
        }
    )
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    order = ProviderOrder(
        provider="bridgers",
        provider_order_id="bridgers-order-42",
        provider_reference="quote-reference-not-a-tx-hash",
        tx_hash="0xdeposit",
    )

    status = await provider.get_status(order)

    assert transport.calls[0]["payload"] == {"orderId": "bridgers-order-42"}
    assert status.status == "refunded"
    assert status.provider_payload == {
        "provider_status": "refund_complete",
        "refund_amount": "9.9",
        "refund_tx_hash": "0xrefund",
        "refund_explorer_url": "https://example.invalid/refund",
        "refund_reason": "slippage",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_status", ["ERROR", "error"])
async def test_bridgers_error_status_is_processing(provider_status: str):
    provider = BridgersProvider.from_transport(
        FakeTransport(
            {
                "resCode": 100,
                "resMsg": "success",
                "data": {"orderId": "order-1", "status": provider_status},
            }
        ),
        source_flag="wallet-agent",
    )
    order = ProviderOrder(
        provider="bridgers", provider_order_id="order-1", provider_reference="quote-1"
    )

    assert (await provider.get_status(order)).status == "processing"
