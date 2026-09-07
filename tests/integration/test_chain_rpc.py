import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chain_rpc_integration_is_explicitly_opt_in():
    pytest.skip("RPC integration requires a configured sandbox endpoint")
