from decimal import Decimal
from typing import Any

import pytest

from wallet_agent.domain.models import Asset, AssetQuery, ProviderOrder, SwapQuoteRequest
from wallet_agent.providers.http import ProviderResponseError
from wallet_agent.providers.omnibridge import OmniBridgeProvider


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


def valid_quote_request(amount: str = "1.5") -> SwapQuoteRequest:
    amount_decimal = Decimal(amount)
    return SwapQuoteRequest(
        source_asset=Asset(chain="ETH", chain_id=1, symbol="ETH", decimals=18),
        destination_asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
        input_amount=amount_decimal,
        input_amount_raw=str(int(amount_decimal * Decimal(10**18))),
        sender_address="0x1234567890abcdef1234567890abcdef12345678",
        recipient_address="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        refund_address="0x1234567890abcdef1234567890abcdef12345678",
        slippage_bps=200,
    )


def quote_response(**overrides: Any) -> dict[str, Any]:
    data = {
        "chainFee": "0.001",
        "depositCoinFeeRate": "0.002",
        "depositMax": "14",
        "depositMin": "0.038603",
        "instantRate": "6.875775974236",
        "burnRate": "0",
        "isSupportNoGas": True,
        "isSupport": True,
        "difference": "0.1",
    }
    data.update(overrides)
    return {"resCode": "800", "resMsg": "Success", "data": data}


@pytest.mark.asyncio
async def test_omnibridge_list_assets_maps_documented_array_shape_and_filters():
    transport = FakeTransport(
        {
            "resCode": "800",
            "resMsg": "Success",
            "data": [
                {
                    "mainNetwork": "ETH",
                    "coinCode": "USDT",
                    "coinDecimal": "6",
                    "contact": "0x0000000000000000000000000000000000000011",
                    "coinImageUrl": "https://example.invalid/usdt.png",
                },
                {
                    "mainNetwork": "BSC",
                    "coinCode": "USDC",
                    "coinDecimal": 18,
                    "contact": "0x0000000000000000000000000000000000000022",
                },
            ],
        }
    )
    provider = OmniBridgeProvider.from_transport(transport, source_flag="wallet-agent")

    assets = await provider.list_assets(AssetQuery(chain="eth", search="usdt"))

    assert assets == [
        Asset(
            chain="ETH",
            symbol="USDT",
            decimals=6,
            address="0x0000000000000000000000000000000000000011",
            logo_url="https://example.invalid/usdt.png",
        )
    ]
    assert transport.calls[0]["payload"] == {
        "sourceFlag": "wallet-agent",
        "mainNetwork": "eth",
    }


@pytest.mark.asyncio
async def test_omnibridge_quote_payload_contains_only_normalized_resume_metadata():
    provider = OmniBridgeProvider.from_transport(
        FakeTransport(
            quote_response(),
            {
                "resCode": "800",
                "resMsg": "Success",
                "data": {"orderId": "resume-order", "platformAddr": "0xplatform"},
            },
        ),
        source_flag="wallet-agent",
        source_type="IOS",
    )

    quote = await provider.quote(valid_quote_request())

    assert quote.provider_payload == {
        "equipment_no": "0x1234567890abcdef1234567890abcd",
        "source_flag": "wallet-agent",
        "source_type": "IOS",
        "destination_addr": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        "refund_addr": "0x1234567890abcdef1234567890abcdef12345678",
        "slippage_bps": 200,
        "deposit_min": "0.038603",
        "deposit_max": "14",
    }
    assert "request" not in quote.provider_payload
    assert "quote_data" not in quote.provider_payload

    provider._quotes.clear()
    order = await provider.prepare(quote)
    assert order.provider_order_id


@pytest.mark.asyncio
async def test_omnibridge_800_quote_normalizes_amounts_and_preserves_source_flag():
    transport = FakeTransport(quote_response())
    provider = OmniBridgeProvider.from_transport(
        transport, source_flag="wallet-agent", source_type="IOS"
    )

    quote = await provider.quote(valid_quote_request())

    assert quote.expected_output == Decimal("10.2920366334312920")
    assert quote.expected_output_raw == "10292036633431292000"
    assert quote.provider_fee == Decimal("0.003")
    assert quote.network_fee == Decimal("0.001")
    assert transport.calls[0] == {
        "path": "/api/v1/getBaseInfo",
        "payload": {
            "depositCoinCode": "ETH",
            "receiveCoinCode": "BNB(BSC)",
            "depositCoinAmt": "1.5",
            "sourceFlag": "wallet-agent",
        },
        "idempotency_key": None,
    }


@pytest.mark.asyncio
async def test_omnibridge_rejects_non_800_response():
    provider = OmniBridgeProvider.from_transport(
        FakeTransport({"resCode": 801, "resMsg": "Unsupported", "data": {}}),
        source_flag="wallet-agent",
    )

    with pytest.raises(ProviderResponseError) as exc_info:
        await provider.quote(valid_quote_request())

    assert exc_info.value.code == "801"
    assert not exc_info.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "minimum", "maximum"),
    [("0.03", "0.038603", "14"), ("15", "0.038603", "14")],
)
async def test_omnibridge_validates_amount_bounds_before_order_creation(
    amount: str, minimum: str, maximum: str
):
    transport = FakeTransport(quote_response(depositMin=minimum, depositMax=maximum))
    provider = OmniBridgeProvider.from_transport(transport, source_flag="wallet-agent")
    quote = await provider.quote(valid_quote_request(amount))

    with pytest.raises(ValueError, match="deposit amount"):
        await provider.prepare(quote)

    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_omnibridge_prepare_creates_deposit_order_from_platform_address():
    transport = FakeTransport(
        quote_response(),
        {
            "resCode": 800,
            "resMsg": "Success",
            "data": {
                "orderId": "omni-order-42",
                "platformAddr": "0x0000000000000000000000000000000000000099",
                "depositCoinCode": "ETH",
                "receiveCoinCode": "BNB(BSC)",
                "depositCoinAmt": "1.5",
                "receiveCoinAmt": "10.2920366334312920",
                "destinationAddr": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
                "refundAddr": "0x1234567890abcdef1234567890abcdef12345678",
                "detailState": "wait_deposit_send",
            },
        },
    )
    provider = OmniBridgeProvider.from_transport(
        transport, source_flag="wallet-agent", source_type="IOS"
    )
    quote = await provider.quote(valid_quote_request())

    order = await provider.prepare(quote)

    assert order.provider_order_id == "omni-order-42"
    assert order.deposit_address == "0x0000000000000000000000000000000000000099"
    assert order.provider_reference == "omni-order-42"
    assert transport.calls[1]["idempotency_key"] == quote.provider_reference
    assert transport.calls[1]["payload"] == {
        "depositCoinCode": "ETH",
        "receiveCoinCode": "BNB(BSC)",
        "depositCoinAmt": "1.5",
        "receiveCoinAmt": "10.2920366334312920",
        "destinationAddr": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        "refundAddr": "0x1234567890abcdef1234567890abcdef12345678",
        "equipmentNo": "0x1234567890abcdef1234567890abcd",
        "sourceType": "IOS",
        "sourceFlag": "wallet-agent",
        "slippage": "0.02",
    }


@pytest.mark.asyncio
async def test_omnibridge_register_broadcast_calls_modify_tx_id_idempotently():
    transport = FakeTransport(
        quote_response(),
        {
            "resCode": 800,
            "resMsg": "Success",
            "data": {
                "orderId": "omni-order-42",
                "platformAddr": "0x0000000000000000000000000000000000000099",
            },
        },
        {"resCode": "800", "resMsg": "Success", "data": "SUCCESS"},
    )
    provider = OmniBridgeProvider.from_transport(transport, source_flag="wallet-agent")
    deposit_order = await provider.prepare(await provider.quote(valid_quote_request()))

    order = await provider.register_broadcast(deposit_order.provider_reference, "0xdeposit")

    assert order.provider_order_id == "omni-order-42"
    assert order.tx_hash == "0xdeposit"
    assert transport.calls[2]["path"] == "/api/v2/modifyTxId"
    assert transport.calls[2]["payload"] == {
        "orderId": "omni-order-42",
        "depositTxid": "0xdeposit",
    }
    assert transport.calls[2]["idempotency_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("detail_state", ["ERROR", "error"])
async def test_omnibridge_error_detail_state_is_processing(detail_state: str):
    transport = FakeTransport(
        {
            "resCode": 800,
            "resMsg": "Success",
            "data": {
                "orderId": "omni-order-42",
                "detailState": detail_state,
                "refundAddr": "0xrefund",
                "refundCoinAmt": "",
                "refundDepositTxid": "",
                "refundReason": "",
            },
        }
    )
    provider = OmniBridgeProvider.from_transport(
        transport, source_flag="wallet-agent", source_type="H5"
    )
    order = ProviderOrder(
        provider="omnibridge",
        provider_order_id="omni-order-42",
        provider_reference="omni-order-42",
        provider_payload={
            "equipment_no": "equipment-42",
            "source_type": "H5",
        },
    )

    status = await provider.get_status(order)

    assert status.status == "processing"
    assert transport.calls[0]["payload"] == {
        "equipmentNo": "equipment-42",
        "sourceType": "H5",
        "orderId": "omni-order-42",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail_state", "expected"),
    [
        ("wait_deposit_send", "pending"),
        ("wait_exchange_return", "processing"),
        ("receive_complete", "completed"),
        ("refund_complete", "refunded"),
        ("timeout", "timed_out"),
        ("WAIT_KYC", "kyc_required"),
    ],
)
async def test_omnibridge_normalizes_detailed_terminal_statuses(detail_state: str, expected: str):
    provider = OmniBridgeProvider.from_transport(
        FakeTransport(
            {
                "resCode": 800,
                "resMsg": "Success",
                "data": {
                    "orderId": "omni-order-42",
                    "detailState": detail_state,
                    "transactionId": "0xreceive",
                    "refundAddr": "0xrefund",
                    "refundCoinAmt": "1.4",
                    "refundDepositTxid": "0xrefundtx",
                    "refundReason": "2",
                    "completeTime": "2026-09-04 10:30:00",
                    "kycUrl": "https://example.invalid/kyc",
                },
            }
        ),
        source_flag="wallet-agent",
    )
    order = ProviderOrder(
        provider="omnibridge",
        provider_order_id="omni-order-42",
        provider_reference="omni-order-42",
        provider_payload={"equipment_no": "equipment-42", "source_type": "H5"},
    )

    status = await provider.get_status(order)

    assert status.status == expected
    assert status.refund_address == "0xrefund"
    assert status.provider_payload == {
        "provider_status": detail_state,
        "receive_tx_hash": "0xreceive",
        "refund_amount": "1.4",
        "refund_tx_hash": "0xrefundtx",
        "refund_reason": "2",
        "kyc_url": "https://example.invalid/kyc",
    }
    if expected in {"completed", "refunded"}:
        assert status.completed_at is not None
