import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_integration_is_explicitly_opt_in():
    pytest.skip("provider integration requires a configured sandbox endpoint")
