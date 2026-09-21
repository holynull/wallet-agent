from decimal import Decimal

import pytest

from wallet_agent.domain.models import (
    Asset,
    FeeEstimate,
    GasLimitEstimate,
    SimulationResult,
    TokenBalance,
    UnsignedTransaction,
)
from wallet_agent.graph.nodes import _transaction_preflight

OWNER = "0x" + "1" * 40
RECIPIENT = "0x" + "2" * 40


class Adapter:
    async def validate_address(self, address):
        return address.startswith("0x") and len(address) == 42

    async def estimate_fee(self, **kwargs):
        return FeeEstimate(
            chain="BASE",
            chain_id=8453,
            asset=Asset(chain="BASE", symbol="ETH", decimals=18),
            amount=Decimal("0.000001"),
            amount_raw="100",
            gas_limit="21000",
            max_fee_per_gas="10",
            max_priority_fee_per_gas="2",
        )

    async def get_native_balance(self, address):
        return TokenBalance(
            asset=Asset(chain="BASE", symbol="ETH", decimals=18),
            amount=Decimal("1"),
            amount_raw="1000000",
        )


class Okx:
    async def estimate_gas_limit(self, transaction):
        return GasLimitEstimate(gas_limit="30000", chain=transaction.chain)

    async def simulate_transaction(self, transaction):
        return SimulationResult(success=True, gas_used="25000", chain=transaction.chain)


@pytest.mark.asyncio
async def test_preflight_uses_maximum_rpc_okx_and_simulation_gas_with_sources():
    tx = UnsignedTransaction(chain="BASE", to=RECIPIENT, data="0x", value="0")
    result = await _transaction_preflight(
        adapter=Adapter(),
        chain="BASE",
        sender=OWNER,
        recipient=RECIPIENT,
        amount_raw="1",
        token=None,
        transaction=tx,
        wallet_context={"address": OWNER, "chain": "BASE"},
        wallet_provider=Okx(),
    )
    assert result["ok"] is True
    assert result["fee_estimate"]["gas_limit"] == "30000"
    assert result["gas_sources"]["okx"] == "30000"
    assert result["gas_sources"]["simulation"] == "25000"
    assert result["simulation"]["success"] is True


@pytest.mark.asyncio
async def test_preflight_blocks_explicit_simulation_failure_but_warns_when_unavailable():
    class FailedOkx(Okx):
        async def simulate_transaction(self, transaction):
            return SimulationResult(
                success=False,
                failure_reason="reverted",
                chain=transaction.chain,
            )

    tx = UnsignedTransaction(chain="BASE", to=RECIPIENT, data="0x", value="0")
    failed = await _transaction_preflight(
        adapter=Adapter(), chain="BASE", sender=OWNER, recipient=RECIPIENT,
        amount_raw="1", token=None, transaction=tx,
        wallet_context={"address": OWNER, "chain": "BASE"}, wallet_provider=FailedOkx(),
    )
    assert failed["ok"] is False
    assert any(item.get("code") == "SIMULATION_FAILED" for item in failed["checks"])

    class Unavailable(Okx):
        async def simulate_transaction(self, transaction):
            raise RuntimeError("offline")

    available = await _transaction_preflight(
        adapter=Adapter(), chain="BASE", sender=OWNER, recipient=RECIPIENT,
        amount_raw="1", token=None, transaction=tx,
        wallet_context={"address": OWNER, "chain": "BASE"}, wallet_provider=Unavailable(),
    )
    assert available["ok"] is True
    assert any(item.get("code") == "SIMULATION_UNAVAILABLE" for item in available["warnings"])
