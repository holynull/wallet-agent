import pytest

from wallet_agent.chains.execution_observer import ExecutionObserver


class Adapter:
    async def get_allowance(self, *args):
        return "7"

    async def get_transaction_receipt(self, tx_hash):
        return {"hash": tx_hash}

    async def get_transaction(self, tx_hash):
        return {"hash": tx_hash}

    async def get_transaction_count(self, address, block):
        return 3

    async def estimate_fee(self, **kwargs):
        return kwargs

    async def get_transaction_status(self, tx_hash):
        return tx_hash


class Registry:
    def __init__(self):
        self.adapter_instance = Adapter()

    def get_adapter(self, chain):
        assert chain == "ETH"
        return self.adapter_instance


@pytest.mark.asyncio
async def test_execution_observer_delegates_only_to_selected_chain_adapter():
    observer = ExecutionObserver(Registry())
    assert await observer.get_allowance("ETH", "token", "owner", "spender") == "7"
    assert await observer.get_transaction_receipt("ETH", "0xabc") == {"hash": "0xabc"}
    assert await observer.get_transaction("ETH", "0xabc") == {"hash": "0xabc"}
    assert await observer.get_transaction_count("ETH", "owner") == 3
    assert await observer.estimate_fee("ETH", to="0xdef") == {"to": "0xdef"}
    assert await observer.get_transaction_status("ETH", "0xabc") == "0xabc"
