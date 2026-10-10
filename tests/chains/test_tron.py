import pytest

from wallet_agent.chains.tron import TronChainAdapter
from wallet_agent.domain.models import Asset, TransactionStatus, UnsignedTransaction

OWNER = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
RECIPIENT = "TFutBMkB47rYtn5EwECfnfDgDmkUP361Sy"
RECIPIENT_WORD = "0" * 24 + "412fcb6e2f0a4b8f1c9ecb1f8d1a0c3b8f2e7d55"
AMOUNT_WORD = "0" * 58 + "16e360"
USDT = Asset(chain="TRON", symbol="USDT", decimals=6, address=OWNER)


class WriteHttpFake:
    """Captures POST bodies and returns TRON node shaped payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def post(self, path, **kwargs):
        payload = kwargs.get("json") or {}
        self.calls.append((path, payload))
        if path.endswith("/createtransaction"):
            return {"txID": "aa" * 32, "raw_data_hex": "0a02dead"}
        if path.endswith("/triggersmartcontract"):
            return {
                "result": {"result": True},
                "transaction": {"txID": "bb" * 32, "raw_data_hex": "0a02beef"},
            }
        if path.endswith("/triggerconstantcontract"):
            return {"constant_result": ["0" * 62 + "0f"]}
        return {}


class HttpFake:
    async def get(self, path, **kwargs):
        if path.endswith("/transactions"):
            return {"data": [{"txID": "abc", "ret": [{"contractRet": "SUCCESS"}]}]}
        if path.startswith("/wallet/gettransactionbyid"):
            return {"ret": [{"contractRet": "SUCCESS"}]}
        if path.endswith("getchainparameters"):
            return {"chainParameter": [{"key": "getTransactionFee", "value": 1000}]}
        return {"data": [{"balance": 1_000_000, "trc20": [{"Ttoken": "1000000"}]}]}


async def test_tron_read_only_methods():
    adapter = TronChainAdapter(HttpFake())
    assert await adapter.validate_address("T" + "1" * 33) is False
    assert (await adapter.get_native_balance("Tfake")).amount == 1
    assert (await adapter.get_token_balances("Tfake"))[0].amount == 1
    assert (await adapter.get_transaction_history("Tfake"))[0].status == TransactionStatus.CONFIRMED
    assert (await adapter.estimate_fee()).amount_raw == "1000"
    assert await adapter.get_transaction_status("abc") == TransactionStatus.CONFIRMED


async def test_tron_build_native_transfer_returns_unsigned_transaction():
    transport = WriteHttpFake()
    adapter = TronChainAdapter(transport)

    tx = await adapter.build_native_transfer(
        from_address=OWNER, to_address=RECIPIENT, amount_raw="1500000"
    )

    path, payload = transport.calls[0]
    assert path.endswith("/wallet/createtransaction")
    assert payload == {
        "owner_address": OWNER,
        "to_address": RECIPIENT,
        "amount": 1500000,
        "visible": True,
    }
    assert isinstance(tx, UnsignedTransaction)
    assert tx.chain == "TRON"
    assert tx.to == RECIPIENT
    assert tx.data == "0a02dead"
    assert tx.value == "1500000"
    assert tx.provider_reference == "aa" * 32
    assert tx.display["action"] == "native_transfer"


async def test_tron_build_erc20_transfer_encodes_trc20_calldata():
    transport = WriteHttpFake()
    adapter = TronChainAdapter(transport)

    tx = await adapter.build_erc20_transfer(
        token=USDT, from_address=OWNER, to_address=RECIPIENT, amount_raw="1500000"
    )

    path, payload = transport.calls[0]
    assert path.endswith("/wallet/triggersmartcontract")
    assert payload["contract_address"] == OWNER
    assert payload["data"] == "a9059cbb" + RECIPIENT_WORD + AMOUNT_WORD
    assert payload["fee_limit"] == 100_000_000
    assert tx.data == "0a02beef"
    assert tx.display["action"] == "trc20_transfer"
    assert tx.display["contract"] == OWNER


async def test_tron_build_erc20_approve_encodes_spender():
    transport = WriteHttpFake()
    adapter = TronChainAdapter(transport)

    tx = await adapter.build_erc20_approve(
        token=USDT, owner=OWNER, spender=RECIPIENT, amount_raw="1500000"
    )

    _path, payload = transport.calls[0]
    assert payload["data"] == "095ea7b3" + RECIPIENT_WORD + AMOUNT_WORD
    assert payload["owner_address"] == OWNER
    assert tx.display["action"] == "trc20_approve"
    assert tx.display["spender"] == RECIPIENT


async def test_tron_get_allowance_reads_constant_result():
    transport = WriteHttpFake()
    adapter = TronChainAdapter(transport)

    assert await adapter.get_allowance(USDT, OWNER, RECIPIENT) == "15"


async def test_tron_build_rejects_invalid_addresses_and_missing_payloads():
    adapter = TronChainAdapter(WriteHttpFake())
    with pytest.raises(ValueError):
        await adapter.build_native_transfer(
            from_address=OWNER, to_address="not-a-tron-address", amount_raw="1"
        )

    class EmptyFake(WriteHttpFake):
        async def post(self, path, **kwargs):
            return {}

    with pytest.raises(ValueError):
        await TronChainAdapter(EmptyFake()).build_native_transfer(
            from_address=OWNER, to_address=RECIPIENT, amount_raw="1"
        )
