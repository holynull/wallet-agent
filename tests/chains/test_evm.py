import pytest

from wallet_agent.chains.evm import EVMChainAdapter
from wallet_agent.domain.models import Asset, TransactionStatus, UnsignedTransaction


class RpcFake:
    def __init__(self):
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        values = {
            "eth_getBalance": "0x16345785d8a0000",  # 0.1 ETH
            "eth_call": "0x0f4240",
            "eth_estimateGas": "0x5208",
            "eth_gasPrice": "0x3b9aca00",
            "eth_getTransactionReceipt": {"status": "0x1", "blockNumber": "0x10"},
            "wallet_getTransactionHistory": [
                {"hash": "0xabc", "from": "0x1", "to": "0x2", "value": "0x1", "blockNumber": "0x10"}
            ],
        }
        return values[method]


class RecordingRpc:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        return self.responses[method]


OWNER = "0x" + "1" * 40
SPENDER = "0x" + "2" * 40
RECIPIENT = "0x" + "3" * 40
USDC = Asset(chain="EVM", symbol="USDC", decimals=6, address="0x" + "4" * 40)
BAD_TOKEN = Asset(chain="EVM", symbol="BAD", decimals=6, address="0x123")


def test_build_native_transfer_uses_unsigned_transfer_shape():
    adapter = EVMChainAdapter(RpcFake())

    tx = adapter.build_native_transfer(from_address=OWNER, to_address=RECIPIENT, amount_raw="123")

    assert isinstance(tx, UnsignedTransaction)
    assert tx.to == RECIPIENT
    assert tx.data == "0x"
    assert tx.value == "123"


def test_build_erc20_transfer_encodes_standard_selector():
    adapter = EVMChainAdapter(RpcFake())

    tx = adapter.build_erc20_transfer(
        token=USDC,
        from_address=OWNER,
        to_address=RECIPIENT,
        amount_raw="15",
    )

    assert tx.to == USDC.address
    assert tx.data == "0xa9059cbb" + ("0" * 24) + RECIPIENT.removeprefix("0x") + ("0" * 63) + "f"


def test_build_erc20_approve_encodes_standard_selector():
    adapter = EVMChainAdapter(RpcFake())

    tx = adapter.build_erc20_approve(
        token=USDC,
        owner=OWNER,
        spender=SPENDER,
        amount_raw="1000000",
    )

    assert tx.to == USDC.address
    assert tx.data == "0x095ea7b3" + ("0" * 24) + SPENDER.removeprefix("0x") + ("0" * 59) + "f4240"


def test_build_erc20_transfer_rejects_malformed_address():
    adapter = EVMChainAdapter(RpcFake())

    with pytest.raises(ValueError):
        adapter.build_erc20_transfer(
            token=USDC,
            from_address=OWNER,
            to_address="0x123",
            amount_raw="15",
        )


def test_build_erc20_approve_rejects_negative_amount():
    adapter = EVMChainAdapter(RpcFake())

    with pytest.raises(ValueError):
        adapter.build_erc20_approve(
            token=USDC,
            owner=OWNER,
            spender=SPENDER,
            amount_raw="-1",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda adapter: adapter.get_token_balance(BAD_TOKEN, OWNER),
        lambda adapter: adapter.get_allowance(BAD_TOKEN, OWNER, SPENDER),
    ],
)
async def test_malformed_token_address_is_rejected_for_reads(call):
    adapter = EVMChainAdapter(RpcFake())

    with pytest.raises(ValueError):
        await call(adapter)


@pytest.mark.parametrize(
    "call",
    [
        lambda adapter: adapter.build_erc20_transfer(
            token=BAD_TOKEN,
            from_address=OWNER,
            to_address=RECIPIENT,
            amount_raw="15",
        ),
        lambda adapter: adapter.build_erc20_approve(
            token=BAD_TOKEN,
            owner=OWNER,
            spender=SPENDER,
            amount_raw="15",
        ),
    ],
)
def test_malformed_token_address_is_rejected_for_builders(call):
    adapter = EVMChainAdapter(RpcFake())

    with pytest.raises(ValueError):
        call(adapter)


@pytest.mark.asyncio
async def test_get_token_balance_reads_standard_balance_of():
    rpc = RecordingRpc({"eth_call": "0x2a"})
    adapter = EVMChainAdapter(rpc, token_assets=[USDC])

    balance = await adapter.get_token_balance(USDC, OWNER)

    assert balance.amount_raw == "42"
    assert rpc.calls == [
        ("eth_call", [{"to": USDC.address, "data": "0x70a08231" + ("0" * 24) + "1" * 40}, "latest"])
    ]


@pytest.mark.asyncio
async def test_allowance_reads_owner_and_spender_words():
    rpc = RecordingRpc({"eth_call": "0x" + ("0" * 63) + "f"})
    adapter = EVMChainAdapter(rpc)

    assert await adapter.get_allowance(USDC, OWNER, SPENDER) == "15"
    assert rpc.calls == [
        (
            "eth_call",
            [
                {
                    "to": USDC.address,
                    "data": "0xdd62ed3e" + ("0" * 24) + "1" * 40 + ("0" * 24) + "2" * 40,
                },
                "latest",
            ],
        )
    ]


@pytest.mark.asyncio
async def test_get_transaction_receipt_returns_none_for_missing_receipt():
    rpc = RecordingRpc({"eth_getTransactionReceipt": None})
    adapter = EVMChainAdapter(rpc)

    assert await adapter.get_transaction_receipt("0xabc") is None

async def test_evm_read_only_methods_and_units():
    rpc = RpcFake()
    token = Asset(chain="EVM", symbol="USDC", decimals=6, address="0x" + "2" * 40)
    adapter = EVMChainAdapter(rpc, chain_id=1, token_assets=[token])

    assert await adapter.validate_address("0x" + "1" * 40)
    assert not await adapter.validate_address("0x123")
    assert (await adapter.get_native_balance("0x" + "1" * 40)).amount_raw == "100000000000000000"
    assert (await adapter.get_token_balances("0x" + "1" * 40))[0].amount == 1
    assert (await adapter.get_transaction_history("0x" + "1" * 40))[
        0
    ].status == TransactionStatus.CONFIRMED
    assert (await adapter.estimate_fee()).amount_raw == str(21000 * 1_000_000_000)
    assert await adapter.get_transaction_status("0xabc") == TransactionStatus.CONFIRMED


async def test_evm_fee_estimate_uses_transaction_context_and_returns_wallet_gas_fields():
    rpc = RecordingRpc(
        {
            "eth_estimateGas": "0x7530",
            "eth_gasPrice": "0x5f5e100",
            "eth_maxPriorityFeePerGas": "0x1e8480",
        }
    )
    adapter = EVMChainAdapter(rpc, chain="ETH", chain_id=1)

    fee = await adapter.estimate_fee(
        to=RECIPIENT,
        data="0xabc",
        from_address=OWNER,
        value="0",
    )

    assert rpc.calls[0] == (
        "eth_estimateGas",
        [{"from": OWNER, "to": RECIPIENT, "data": "0xabc", "value": "0x0"}],
    )
    assert fee.gas_limit == "30000"
    assert fee.max_fee_per_gas == "100000000"
    assert fee.max_priority_fee_per_gas == "2000000"
    assert fee.amount_raw == str(30000 * 100000000)


@pytest.mark.asyncio
async def test_evm_fee_estimate_keeps_max_fee_above_latest_base_fee():
    rpc = RecordingRpc(
        {
            "eth_estimateGas": "0x5208",
            # Deliberately stale/lower than the latest block base fee.
            "eth_gasPrice": "0x6d3d267",
            "eth_maxPriorityFeePerGas": "0x0",
            "eth_getBlockByNumber": {"baseFeePerGas": "0x7497670"},
        }
    )
    adapter = EVMChainAdapter(rpc, chain="ETH", chain_id=1)

    fee = await adapter.estimate_fee(to=RECIPIENT, data="0xabc", from_address=OWNER)

    assert fee.max_fee_per_gas == str(2 * int("0x7497670", 16))
    assert int(fee.max_fee_per_gas) > int("0x7497670", 16)
    assert fee.amount_raw == str(21000 * int(fee.max_fee_per_gas))
