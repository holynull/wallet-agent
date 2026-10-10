"""Design v1.6.9 address-risk card: deterministic recipient risk in preflight."""

from decimal import Decimal

import pytest

from wallet_agent.domain.models import Asset, FeeEstimate, TokenBalance
from wallet_agent.graph.nodes import _address_risk, _transaction_preflight

EVM = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"
OTHER_OWN = "0x" + "9" * 40
EXTERNAL = "0x" + "2" * 40
ZERO = "0x" + "0" * 40
BTC = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"

CONTEXT = {
    "address": EVM,
    "chain": "BASE",
    "wallet_accounts": [
        {"chain": "BASE", "address": EVM},
        {"chain": "BASE", "address": OTHER_OWN},
        {"chain": "BTC", "address": BTC},
    ],
}


class FakeAdapter:
    """Minimal EVM-shaped adapter so preflight can run its checks."""

    transaction_model = "evm"

    async def validate_address(self, _address):
        return True

    async def estimate_fee(self, **_kwargs):
        return FeeEstimate(
            chain="BASE",
            asset=Asset(chain="BASE", symbol="ETH", decimals=18),
            amount=Decimal("0.0001"),
            amount_raw="100000000000000",
        )

    async def get_native_balance(self, _address):
        return TokenBalance(
            asset=Asset(chain="BASE", symbol="ETH", decimals=18),
            amount=Decimal("1"),
            amount_raw="1000000000000000000",
        )


async def _preflight(recipient: str):
    return await _transaction_preflight(
        adapter=FakeAdapter(),
        chain="BASE",
        sender=EVM,
        recipient=recipient,
        amount_raw="1000000000000000",
        token=None,
        transaction=None,
        wallet_context=CONTEXT,
    )


def test_address_risk_detects_burn_self_and_own_addresses():
    assert _address_risk(EVM, ZERO, "BASE", CONTEXT)["code"] == "ZERO_ADDRESS_RECIPIENT"
    assert _address_risk(EVM, EVM, "BASE", CONTEXT)["code"] == "SELF_TRANSFER"
    assert _address_risk(EVM, OTHER_OWN, "BASE", CONTEXT)["code"] == "OWN_ADDRESS_RECIPIENT"
    assert _address_risk(EVM, EXTERNAL, "BASE", CONTEXT) is None
    # The zero-address rule is EVM-specific and must not fire on Bitcoin.
    assert _address_risk(BTC, ZERO, "BTC", CONTEXT) is None


@pytest.mark.asyncio
async def test_self_transfer_is_a_warning_that_still_prepares():
    result = await _preflight(EVM)

    check = next(item for item in result["checks"] if item["name"] == "address_risk")
    assert check["status"] == "warning"
    assert check["code"] == "SELF_TRANSFER"
    assert result["risk"]["level"] == "warning"
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_burn_address_blocks_the_transfer():
    result = await _preflight(ZERO)

    check = next(item for item in result["checks"] if item["name"] == "address_risk")
    assert check["status"] == "failed"
    assert check["code"] == "ZERO_ADDRESS_RECIPIENT"
    assert result["risk"]["level"] == "danger"
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_clear_recipient_reports_no_risk():
    result = await _preflight(EXTERNAL)

    check = next(item for item in result["checks"] if item["name"] == "address_risk")
    assert check["status"] == "passed"
    assert result["risk"] is None
    assert result["ok"] is True
