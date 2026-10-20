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
    cached_assets = await provider.list_assets(AssetQuery(chain="Ethereum", search="USDC"))

    assert [asset.symbol for asset in assets] == ["USDT(ERC20)"]
    assert [asset.symbol for asset in cached_assets] == ["USDC"]
    assert len(transport.calls) == 1
    assert transport.calls[0]["payload"] == {"chain": "ETH"}


@pytest.mark.asyncio
async def test_bridgers_list_assets_uses_trx_filter_for_tron():
    """Bridgers' getToken filter spells TRON as ``TRX`` (catalog entries still
    report ``chain: TRON``). Sending ``TRON`` returns zero tokens and made an
    ETH USDT → TRX swap fail to resolve its token address (Lif report)."""
    transport = FakeTransport(
        {
            "resCode": "100",
            "data": {
                "tokens": [
                    {
                        "chain": "TRON",
                        "symbol": "TRX",
                        "decimals": 6,
                        "address": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                    },
                    {
                        "chain": "TRON",
                        "symbol": "USDT(TRON)",
                        "decimals": 6,
                        "address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                    },
                ]
            },
        }
    )
    provider = BridgersProvider.from_transport(transport)

    assets = await provider.list_assets(AssetQuery(chain="TRON", search="TRX"))

    assert transport.calls[0]["payload"] == {"chain": "TRX"}
    assert [asset.symbol for asset in assets] == ["TRX"]
    assert [asset.chain for asset in assets] == ["TRON"]


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
async def test_bridgers_quote_resolves_native_asset_addresses_from_catalog():
    transport = FakeTransport(
        {
            "resCode": 100,
            "data": {
                "tokens": [
                    {
                        "chain": "BSC",
                        "symbol": "BNB(BSC)",
                        "address": "0x00000000000000000000000000000000000000bb",
                        "decimals": 18,
                    }
                ]
            },
        },
        quote_response(),
        {
            "resCode": 100,
            "resMsg": "success",
            "data": {
                "txData": {
                    "to": "0x00000000000000000000000000000000000000bb",
                    "data": "0x",
                    "value": "0x0",
                }
            },
        },
    )
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    request = valid_quote_request().model_copy(
        update={
            "destination_asset": valid_quote_request().destination_asset.model_copy(
                update={
                    "symbol": "BNB",
                    "chain": "BSC",
                    "chain_id": 56,
                    "address": None,
                    "decimals": 18,
                }
            )
        }
    )

    quote = await provider.quote(request)

    assert transport.calls[0] == {
        "path": "/api/exchangeRecord/getToken",
        "payload": {"chain": "BSC"},
        "idempotency_key": None,
    }
    assert transport.calls[1]["path"] == "/api/sswap/quote"
    assert transport.calls[1]["payload"]["toTokenAddress"] == (
        "0x00000000000000000000000000000000000000bb"
    )
    provider._quotes.clear()
    await provider.prepare(quote)
    assert transport.calls[2]["payload"]["toTokenAddress"] == (
        "0x00000000000000000000000000000000000000bb"
    )


@pytest.mark.asyncio
async def test_bridgers_reverse_quote_binary_searches_source_amount():
    transport = DynamicQuoteTransport()
    provider = BridgersProvider.from_transport(transport)

    quote = await provider.reverse_quote(valid_quote_request(), Decimal("4"))

    assert quote.input_amount == Decimal("5")
    assert quote.expected_output == Decimal("4")
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_bridgers_reverse_quote_corrects_rate_movement_with_bounded_calls():
    class MovingRateTransport(DynamicQuoteTransport):
        async def post(self, path, payload, *, idempotency_key=None):
            result = await super().post(path, payload, idempotency_key=idempotency_key)
            if len(self.calls) == 2:
                result["data"]["txData"]["toTokenAmount"] = "3.99"
                result["data"]["txData"]["amountOutMin"] = "3990000"
            return result

    transport = MovingRateTransport()
    provider = BridgersProvider.from_transport(transport)

    quote = await provider.reverse_quote(valid_quote_request(), Decimal("4"))

    assert quote.expected_output >= Decimal("4")
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_bridgers_reverse_quote_accepts_decimal_noise_within_raw_unit():
    class NoiseTransport(DynamicQuoteTransport):
        async def post(self, path, payload, *, idempotency_key=None):
            result = await super().post(path, payload, idempotency_key=idempotency_key)
            tx = result["data"]["txData"]
            raw = int(payload["fromTokenAmount"])
            if raw >= 5_000_000:
                    tx["toTokenAmount"] = "4.0000000000000001"
            return result

    provider = BridgersProvider.from_transport(NoiseTransport())
    quote = await provider.reverse_quote(valid_quote_request(), Decimal("4"))

    assert quote.expected_output_raw == "4000000"


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
async def test_bridgers_tron_erc20_quote_exposes_allowance_requirement():
    owner = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    tron_contract = "TFutBMkB47rYtn5EwECfnfDgDmkUP361Sy"
    transport = FakeTransport(quote_response(contractAddress=tron_contract))
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    request = valid_quote_request().model_copy(
        update={
            "source_asset": Asset(
                chain="TRON", symbol="USDT", decimals=6, address=owner
            ),
            "sender_address": owner,
            "recipient_address": owner,
        }
    )

    quote = await provider.quote(request)

    assert quote.allowance_requirement is not None
    assert quote.allowance_requirement.spender == tron_contract
    assert quote.allowance_requirement.owner == owner


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
        "from_token_address": "0x0000000000000000000000000000000000000011",
        "to_token_address": "0x0000000000000000000000000000000000000022",
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
async def test_bridgers_prepare_reads_a_utxo_deposit_address_and_memo():
    """Bitcoin deposits arrive as ``toAddress``; there is no ``to`` for them."""
    deposit = "bc1qjn7e2xk99m4g4d4j3hszxqfh4uk585jq5m6dt7"
    transport = FakeTransport(
        quote_response(),
        {
            "resCode": "100",
            "resMsg": "success",
            "data": {
                "txData": {
                    "amount": "100000",
                    "toAddress": deposit,
                    "memo": "Y4c:to:USDT(ERC20):0x1a9d699aee3a56ca49d0cc3b542ae3a37885a3e1",
                }
            },
        },
    )
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    quote = await provider.quote(valid_quote_request())

    transaction = await provider.prepare(quote)

    assert transaction.to == deposit
    assert transaction.display["memo"].startswith("Y4c:to:USDT")


@pytest.mark.asyncio
async def test_bridgers_prepare_rebuilds_tron_swap_calldata_with_fee_limit():
    """TRON swaps come back as ``functionName`` + ``parameter`` + ``options``
    instead of EVM ``{to, data, value}``. The calldata must carry the real
    ``swapEth`` selector (not a plain transfer to the router) and the provider's
    ``feeLimit`` must surface so the graph prices energy correctly."""
    transport = FakeTransport(
        quote_response(),
        {
            "resCode": "100",
            "resMsg": "success",
            "data": {
                "txData": {
                    "tronRouterAddrees": "4158b8a2f4c954a0c54c8e392feee612ec2dbe1585",
                    "functionName": "swapEth(string,string,uint256)",
                    "options": {"feeLimit": 100000000, "callValue": "0x2ebae40"},
                    "parameter": [
                        {"type": "string", "value": "BTC|1dcve2|0.01|bridgers|0"},
                        {"type": "string", "value": "1Nqs6wvbYfZK15GQ43ydsiDU7NDnnWR2tm"},
                        {"type": "uint256", "value": "0x4844"},
                    ],
                    "to": "TJ4KeiGTvTX4wRUgLARiwr8rGLaurmVkdZ",
                }
            },
        },
    )
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    quote = await provider.quote(valid_quote_request())

    transaction = await provider.prepare(quote)

    assert transaction.to == "TJ4KeiGTvTX4wRUgLARiwr8rGLaurmVkdZ"
    assert transaction.data.startswith("0x16b3b4c2")  # swapEth(string,string,uint256)
    assert transaction.value == "0x2ebae40"
    assert transaction.display["fee_limit"] == "100000000"


@pytest.mark.asyncio
async def test_bridgers_prepare_rejects_a_payload_without_a_destination():
    transport = FakeTransport(
        quote_response(),
        {"resCode": "100", "resMsg": "success", "data": {"txData": {"data": "0x"}}},
    )
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    quote = await provider.quote(valid_quote_request())

    with pytest.raises(ProviderResponseError):
        await provider.prepare(quote)


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
async def test_bridgers_register_broadcast_rebuilds_full_request_from_persisted_quote():
    """When the in-memory quote cache is cold, register_broadcast must rebuild
    the full Bridgers payload from the persisted session quote (dict form, as
    the graph state carries it) instead of sending the minimal fallback that
    Bridgers rejects with resCode 101."""
    transport = FakeTransport(
        quote_response(),
        {
            "resCode": 100,
            "resMsg": "success",
            "data": {"orderId": "bridgers-order-77"},
        },
    )
    provider = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    quote = await provider.quote(valid_quote_request())

    # Simulate a cold cache: build a fresh provider instance (no _quotes) and
    # pass the persisted quote as a JSON dict, exactly what the graph stores.
    cold = BridgersProvider.from_transport(transport, source_flag="wallet-agent")
    order = await cold.register_broadcast(
        quote.provider_reference, "0xdeposit", quote=quote.model_dump(mode="json")
    )

    assert order.provider_order_id == "bridgers-order-77"
    payload = transport.calls[-1]["payload"]
    assert payload["hash"] == "0xdeposit"
    for required in (
        "equipmentNo",
        "sourceFlag",
        "fromTokenAddress",
        "toTokenAddress",
        "fromTokenAmount",
        "fromTokenChain",
        "toTokenChain",
        "userAddr",
        "fromCoinCode",
        "toCoinCode",
        "fromAddress",
        "toAddress",
        "amountOutMin",
    ):
        assert payload.get(required), f"rebuilt payload is missing {required}"


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
