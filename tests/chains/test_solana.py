from wallet_agent.chains.solana import SolanaChainAdapter
from wallet_agent.domain.models import TransactionStatus


class RpcFake:
    async def request(self, method, params):
        return {
            "getBalance": {"value": 1_000_000_000},
            "getTokenAccountsByOwner": {"value": []},
            "getSignaturesForAddress": [{"signature": "sig", "slot": 3}],
            "getRecentPrioritizationFees": [{"prioritizationFee": 5000}],
            "getSignatureStatuses": {"value": [{"confirmationStatus": "finalized"}]},
        }[method]


async def test_solana_read_only_methods_and_validation():
    adapter = SolanaChainAdapter(RpcFake())
    assert await adapter.validate_address("1" * 32)
    assert not await adapter.validate_address("not-a-key")
    assert (await adapter.get_native_balance("1" * 32)).amount == 1
    assert (await adapter.get_transaction_history("1" * 32))[0].tx_hash == "sig"
    assert (await adapter.estimate_fee()).amount_raw == "5000"
    assert await adapter.get_transaction_status("sig") == TransactionStatus.CONFIRMED
