from wallet_agent.chains.evm import EVMChainAdapter
from wallet_agent.domain.models import Asset, TransactionStatus


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
