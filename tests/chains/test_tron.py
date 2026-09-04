from wallet_agent.chains.tron import TronChainAdapter
from wallet_agent.domain.models import TransactionStatus


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
