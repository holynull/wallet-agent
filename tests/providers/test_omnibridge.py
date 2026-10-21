from decimal import Decimal
from typing import Any

import pytest

from wallet_agent.domain.models import Asset, AssetQuery, ProviderOrder, SwapQuoteRequest
from wallet_agent.providers.http import ProviderResponseError
from wallet_agent.providers.omnibridge import OmniBridgeProvider, _coin_code


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


def erc20_quote_request() -> SwapQuoteRequest:
    return SwapQuoteRequest(
        source_asset=Asset(
            chain="ETH",
            chain_id=1,
            symbol="USDC",
            decimals=6,
            address="0x0000000000000000000000000000000000000011",
        ),
        destination_asset=Asset(
            chain="ETH",
            chain_id=1,
            symbol="USDT(ERC20)",
            decimals=6,
            address="0x0000000000000000000000000000000000000022",
        ),
        input_amount=Decimal("1"),
        input_amount_raw="1000000",
        sender_address="0x1234567890abcdef1234567890abcdef12345678",
        recipient_address="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
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
                    "mainNetwork": "ETH",
                    "coinCode": "USDC",
                    "coinDecimal": 18,
                    "contact": "0x0000000000000000000000000000000000000022",
                },
            ],
        }
    )
    provider = OmniBridgeProvider.from_transport(transport, source_flag="wallet-agent")

    assets = await provider.list_assets(AssetQuery(chain="Ethereum", search="usdt"))
    cached_assets = await provider.list_assets(AssetQuery(chain="Ethereum", search="USDC"))

    assert assets == [
        Asset(
            chain="ETH",
            symbol="USDT",
            decimals=6,
            address="0x0000000000000000000000000000000000000011",
            logo_url="https://example.invalid/usdt.png",
        )
    ]
    assert [asset.symbol for asset in cached_assets] == ["USDC"]
    assert len(transport.calls) == 1
    assert transport.calls[0]["payload"] == {
        "sourceFlag": "wallet-agent",
        "mainNetwork": "ETH",
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
    assert quote.allowance_requirement is None

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
@pytest.mark.parametrize("chain", ["BTC", "bitcoin"])
async def test_omnibridge_quote_uses_the_bare_native_coin_code(chain: str):
    """Omni rejects ``BTC(BTC)`` with resCode 913: its catalog code is ``BTC``."""
    transport = FakeTransport(quote_response(depositMin="0.000001", depositMax="14"))
    provider = OmniBridgeProvider.from_transport(transport, source_flag="wallet-agent")
    request = SwapQuoteRequest(
        source_asset=Asset(chain=chain, symbol="BTC", decimals=8),
        destination_asset=Asset(
            chain="ETH",
            chain_id=1,
            symbol="USDT(ERC20)",
            decimals=6,
            address="0x0000000000000000000000000000000000000022",
        ),
        input_amount=Decimal("0.0002"),
        input_amount_raw="20000",
        sender_address="bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5",
        recipient_address="0x1234567890abcdef1234567890abcdef12345678",
    )

    await provider.quote(request)

    assert transport.calls[0]["payload"]["depositCoinCode"] == "BTC"
    assert transport.calls[0]["payload"]["receiveCoinCode"] == "USDT(ERC20)"


@pytest.mark.asyncio
async def test_omnibridge_quote_uses_bare_trx_coin_code_for_tron_native():
    """Omni's TRX coin code is bare ``TRX`` (coinCodeShow ``TRX(TRON)``);
    deriving ``TRX(TRON)`` is rejected as “接收货币币种不存在” (Lif: ETH USDT →
    TRX 报价失败)."""
    trx = Asset(chain="TRON", symbol="TRX", decimals=6)
    assert _coin_code(trx) == "TRX"

    transport = FakeTransport(quote_response(depositMin="8", depositMax="285000"))
    provider = OmniBridgeProvider.from_transport(transport, source_flag="wallet-agent")
    request = SwapQuoteRequest(
        source_asset=Asset(
            chain="ETH",
            chain_id=1,
            symbol="USDT(ERC20)",
            decimals=6,
            address="0x0000000000000000000000000000000000000022",
        ),
        destination_asset=trx,
        input_amount=Decimal("10"),
        input_amount_raw="10000000",
        sender_address="0x1234567890abcdef1234567890abcdef12345678",
        recipient_address="TGHF8oHDwsuKU8WX82so9GCYe8iJNtit5q",
    )

    await provider.quote(request)

    assert transport.calls[0]["payload"]["depositCoinCode"] == "USDT(ERC20)"
    assert transport.calls[0]["payload"]["receiveCoinCode"] == "TRX"


@pytest.mark.asyncio
async def test_omnibridge_catalog_skips_coins_without_a_decimal_scale():
    """The live catalog carries null decimals for 14 coins; those must not
    fail the whole catalog for every other asset that shares it."""
    transport = FakeTransport(
        {
            "resCode": "800",
            "resMsg": "Success",
            "data": [
                {"mainNetwork": "XLM", "coinCode": "XLM", "coinDecimal": None},
                {"mainNetwork": "BTC", "coinCode": "BTC", "coinDecimal": 8},
            ],
        }
    )
    provider = OmniBridgeProvider.from_transport(transport, source_flag="wallet-agent")

    assets = await provider.list_assets(AssetQuery())

    assert [asset.symbol for asset in assets] == ["BTC"]


@pytest.mark.asyncio
async def test_omnibridge_quote_truncates_deposit_amount_to_eight_places():
    transport = FakeTransport(
        quote_response(depositMin="0.001"),
        {
            "resCode": 800,
            "resMsg": "Success",
            "data": {"orderId": "precision-order", "platformAddr": "0xplatform"},
        },
    )
    provider = OmniBridgeProvider.from_transport(transport, source_flag="wallet-agent")

    quote = await provider.quote(valid_quote_request("0.013677590544728694"))
    order = await provider.prepare(quote)

    assert quote.input_amount == Decimal("0.01367759")
    assert quote.input_amount_raw == "13677590000000000"
    assert order.input_amount == Decimal("0.01367759")
    assert transport.calls[0]["payload"]["depositCoinAmt"] == "0.01367759"
    assert transport.calls[1]["payload"]["depositCoinAmt"] == "0.01367759"


@pytest.mark.asyncio
async def test_omnibridge_prepare_rejects_stale_high_precision_quote_before_api_call():
    transport = FakeTransport(quote_response(depositMin="0.001", depositMax="14"))
    provider = OmniBridgeProvider.from_transport(transport)
    quote = await provider.quote(valid_quote_request("0.013677590544728694"))
    stale_quote = quote.model_copy(
        update={
            "input_amount": Decimal("0.013677590544728694"),
            "input_amount_raw": "13677590544728694",
        }
    )

    with pytest.raises(ProviderResponseError) as exc_info:
        await provider.prepare(stale_quote)

    assert exc_info.value.code == "919"
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_omnibridge_quote_rejects_amount_below_deposit_minimum():
    """A deposit under Omni's minimum fails at quote time with a clear code.

    Regression for the App-side report where 0.003 ETH (min 0.003302) passed
    the quote and only failed on /confirm as PROVIDER_PREPARE_FAILED with no
    pending transaction.
    """
    transport = FakeTransport(quote_response(depositMin="0.003302", depositMax="14"))
    provider = OmniBridgeProvider.from_transport(transport)
    with pytest.raises(ProviderResponseError) as exc_info:
        await provider.quote(valid_quote_request("0.003"))
    assert exc_info.value.code == "BELOW_MIN_DEPOSIT"
    assert "0.003302" in exc_info.value.message
    assert exc_info.value.category == "client"


@pytest.mark.asyncio
async def test_omnibridge_quote_rejects_amount_above_deposit_maximum():
    transport = FakeTransport(quote_response(depositMin="0.001", depositMax="0.05"))
    provider = OmniBridgeProvider.from_transport(transport)
    with pytest.raises(ProviderResponseError) as exc_info:
        await provider.quote(valid_quote_request("1.5"))
    assert exc_info.value.code == "ABOVE_MAX_DEPOSIT"
    assert exc_info.value.category == "client"


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
async def test_omnibridge_reverse_quote_uses_erc20_codes_and_verifies_forward_output():
    transport = FakeTransport(
        quote_response(instantRate="1.00069", depositCoinFeeRate="0.003", chainFee="0.3897"),
        quote_response(instantRate="1.00069", depositCoinFeeRate="0.003", chainFee="0.3897"),
    )
    provider = OmniBridgeProvider.from_transport(transport)

    quote = await provider.reverse_quote(erc20_quote_request(), Decimal("5"))

    assert quote.expected_output >= Decimal("5")
    assert transport.calls[0]["payload"]["depositCoinCode"] == "USDC"
    assert transport.calls[0]["payload"]["receiveCoinCode"] == "USDT(ERC20)"
    assert transport.calls[1]["payload"]["depositCoinAmt"] != "0"


@pytest.mark.asyncio
async def test_omnibridge_reverse_quote_accepts_decimal_noise_within_raw_unit():
    transport = FakeTransport(
        quote_response(instantRate="1", depositCoinFeeRate="0", chainFee="0"),
            quote_response(
                instantRate="1.000000000000000001", depositCoinFeeRate="0", chainFee="0"
            ),
    )
    provider = OmniBridgeProvider.from_transport(transport)

    quote = await provider.reverse_quote(erc20_quote_request(), Decimal("5"))

    assert quote.expected_output > Decimal("4.999999")


@pytest.mark.asyncio
async def test_omnibridge_reverse_quote_rounds_up_to_provider_precision():
    transport = FakeTransport(
        quote_response(
            instantRate="1", depositCoinFeeRate="0", chainFee="0", depositMin="0"
        ),
        quote_response(
            instantRate="1", depositCoinFeeRate="0", chainFee="0", depositMin="0"
        ),
    )
    provider = OmniBridgeProvider.from_transport(transport)

    target = Decimal("0.013677590544728694")
    quote = await provider.reverse_quote(valid_quote_request(), target)

    assert quote.input_amount == Decimal("0.0136776")
    assert quote.input_amount_raw == "13677600000000000"
    assert quote.expected_output >= target
    assert transport.calls[1]["payload"]["depositCoinAmt"] == "0.0136776"


@pytest.mark.asyncio
async def test_omnibridge_ethereum_erc20_quote_exposes_allowance_requirement():
    spender = "0x0000000000000000000000000000000000000033"
    provider = OmniBridgeProvider.from_transport(
        FakeTransport(quote_response(spender=spender)), source_flag="wallet-agent"
    )

    quote = await provider.quote(erc20_quote_request())

    assert quote.allowance_requirement is not None
    assert quote.allowance_requirement.spender == spender


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "minimum", "maximum"),
    [("0.03", "0.038603", "14"), ("15", "0.038603", "14")],
)
async def test_omnibridge_validates_amount_bounds_before_order_creation(
    amount: str, minimum: str, maximum: str
):
    """prepare() keeps the deposit bounds as a safety net for stale/forged quotes.

    A normal quote is now rejected at quote time (BELOW_MIN_DEPOSIT /
    ABOVE_MAX_DEPOSIT); this test forges an out-of-bounds quote past that gate
    to confirm prepare() still refuses it before hitting the provider API.
    """
    transport = FakeTransport(quote_response(depositMin=minimum, depositMax=maximum))
    provider = OmniBridgeProvider.from_transport(transport, source_flag="wallet-agent")
    quote = await provider.quote(valid_quote_request("1"))
    forged = quote.model_copy(update={"input_amount": Decimal(amount)})

    with pytest.raises(ValueError, match="deposit amount"):
        await provider.prepare(forged)

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
async def test_omnibridge_prepare_carries_btc_memo_into_the_deposit_order():
    """Omni returns ``btcMemo`` on the accountExchange response for UTXO
    deposits. The order must surface it so the Bitcoin PSBT gets an OP_RETURN
    output — a plain transfer is never matched (Lif report)."""
    transport = FakeTransport(
        quote_response(),
        {
            "resCode": 800,
            "resMsg": "Success",
            "data": {
                "orderId": "omni-btc-order",
                "platformAddr": "14HwBypSEn5HXFvDGDX6nt1UtnhfDiXcjs",
                "depositCoinCode": "BTC",
                "receiveCoinCode": "BNB(BSC)",
                "depositCoinAmt": "0.0002",
                "receiveCoinAmt": "0.022225",
                "btcMemo": "to:BNB(BSC):0x13c68429D80F56E72C02122652852a1DAfA60AE0",
                "detailState": "wait_deposit_send",
            },
        },
    )
    provider = OmniBridgeProvider.from_transport(
        transport, source_flag="wallet-agent", source_type="IOS"
    )
    quote = await provider.quote(valid_quote_request())

    order = await provider.prepare(quote)

    assert order.memo == "to:BNB(BSC):0x13c68429D80F56E72C02122652852a1DAfA60AE0"
    assert order.provider == "omnibridge"
    assert order.provider_order_id == "omni-btc-order"


@pytest.mark.asyncio
async def test_omnibridge_prepare_leaves_memo_none_when_absent():
    """Non-UTXO deposits have no ``btcMemo``; the order must not invent one."""
    transport = FakeTransport(
        quote_response(),
        {
            "resCode": 800,
            "resMsg": "Success",
            "data": {
                "orderId": "omni-eth-order",
                "platformAddr": "0x0000000000000000000000000000000000000099",
                "depositCoinCode": "ETH",
                "receiveCoinCode": "BNB(BSC)",
                "depositCoinAmt": "1.5",
                "receiveCoinAmt": "10.29",
                "detailState": "wait_deposit_send",
            },
        },
    )
    provider = OmniBridgeProvider.from_transport(
        transport, source_flag="wallet-agent", source_type="IOS"
    )
    quote = await provider.quote(valid_quote_request())

    order = await provider.prepare(quote)

    assert order.memo is None


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
    assert order.provider_payload == {
        "equipment_no": "0x1234567890abcdef1234567890abcd",
        "source_type": "H5",
    }
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
