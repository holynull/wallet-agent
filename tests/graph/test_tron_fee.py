"""TRON TRC-20 fee estimation reaches the preflight with a realistic energy cost."""

import pytest

from wallet_agent.chains.tron import TronChainAdapter
from wallet_agent.domain.models import Asset
from wallet_agent.graph.nodes import _transaction_preflight

OWNER = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
RECIPIENT = "TFutBMkB47rYtn5EwECfnfDgDmkUP361Sy"
USDT = Asset(chain="TRON", symbol="USDT", decimals=6, address=OWNER)


class NodeFake:
    """A TRON node: chain parameters, account balance, and contract simulation."""

    def __init__(self, trx_balance: int) -> None:
        self.trx_balance = trx_balance
        self.trigger_calls = 0

    async def get(self, path, **kwargs):
        if path.endswith("getchainparameters"):
            return {
                "chainParameter": [
                    {"key": "getTransactionFee", "value": 1000},
                    {"key": "getEnergyFee", "value": 420},
                ]
            }
        if path.startswith("/v1/accounts/"):
            return {"data": [{"balance": self.trx_balance}]}
        return {}

    async def post(self, path, **kwargs):
        if path.endswith("/triggersmartcontract"):
            self.trigger_calls += 1
            return {
                "result": {"result": True},
                "transaction": {"txID": "bb" * 32, "raw_data_hex": "0a02beef"},
                "energy_used": 100000,
                "energy_penalty": 11065,
                "energy_used_total": 111065,
                "net_fee": 345000,
                "energy_fee_total": 46647300,
            }
        return {}


@pytest.mark.asyncio
async def test_tron_trc20_preflight_prices_energy_and_fails_low_gas():
    node = NodeFake(trx_balance=5_000_000)  # 5 TRX, far below the ~47 TRX energy fee
    adapter = TronChainAdapter(node)
    tx = await adapter.build_erc20_transfer(
        token=USDT, from_address=OWNER, to_address=RECIPIENT, amount_raw="1000000"
    )
    # Build already simulated once; count only the preflight's fee-estimate call.
    node.trigger_calls = 0

    preflight = await _transaction_preflight(
        adapter=adapter,
        chain="TRON",
        sender=OWNER,
        recipient=RECIPIENT,
        amount_raw="1000000",
        token=USDT,
        transaction=tx,
        wallet_context={"chain": "TRON"},
    )

    # The confirmation card and gas check now see the energy-based fee, not the
    # 0.001 TRX native-transfer bandwidth fee.
    assert preflight["fee_estimate"]["amount_raw"] == "46992300"
    assert node.trigger_calls == 1
    checks = {item["name"]: item["status"] for item in preflight["checks"]}
    assert checks["network_fee"] == "passed"
    assert checks["gas_balance"] == "failed"
    assert preflight["ok"] is False


@pytest.mark.asyncio
async def test_tron_trc20_preflight_passes_when_trx_covers_energy_fee():
    node = NodeFake(trx_balance=50_000_000)  # 50 TRX covers the ~47 TRX energy fee
    adapter = TronChainAdapter(node)
    tx = await adapter.build_erc20_transfer(
        token=USDT, from_address=OWNER, to_address=RECIPIENT, amount_raw="1000000"
    )

    preflight = await _transaction_preflight(
        adapter=adapter,
        chain="TRON",
        sender=OWNER,
        recipient=RECIPIENT,
        amount_raw="1000000",
        token=USDT,
        transaction=tx,
        wallet_context={"chain": "TRON"},
    )

    checks = {item["name"]: item["status"] for item in preflight["checks"]}
    assert checks["gas_balance"] == "passed"
    assert preflight["ok"] is True
