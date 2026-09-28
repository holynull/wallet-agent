import pytest

from wallet_agent.chains.registry import ChainAdapterRegistry, build_default_registry
from wallet_agent.domain.errors import ChainCapabilityUnavailable


def test_registry_exposes_explicit_stubs():
    registry = build_default_registry(include_stubs=True)
    assert registry.get("xrp").capabilities.available == frozenset()


def test_rpc_registry_uses_each_evm_chains_native_gas_asset():
    registry = build_default_registry(
        rpc_urls={"ETH": "https://eth.invalid", "BSC": "https://bsc.invalid"},
        include_stubs=False,
    )

    assert registry.get("ETH").native_asset.symbol == "ETH"
    assert registry.get("BSC").native_asset.symbol == "BNB"


@pytest.mark.asyncio
async def test_stub_rejects_every_operation_with_structured_code():
    adapter = ChainAdapterRegistry({"XRP": build_default_registry().get("XRP")}).get("XRP")
    with pytest.raises(ChainCapabilityUnavailable) as exc:
        await adapter.get_native_balance("r-address")
    assert exc.value.code == "CHAIN_CAPABILITY_UNAVAILABLE"
