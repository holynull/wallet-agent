import os

import pytest

from wallet_agent.config import Settings


@pytest.mark.integration
def test_real_configuration_is_valid_when_opted_in():
    if os.getenv("RUN_INTEGRATION_TESTS") != "1":
        pytest.skip("set RUN_INTEGRATION_TESTS=1 to validate external configuration")
    settings = Settings()
    assert settings.deepseek_api_key or settings.openai_api_key
    assert settings.rpc_urls
