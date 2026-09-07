import os

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deepseek_smoke():
    """Opt-in smoke test; never runs in the default unit-test suite."""
    if not os.getenv("RUN_INTEGRATION_TESTS"):
        pytest.skip("set RUN_INTEGRATION_TESTS=1 to enable external calls")
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY is not configured")
    from langchain_openai import ChatOpenAI

    response = await ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "deepseek-chat"),
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com"),
    ).ainvoke("Reply with the single word: ok")
    assert response.content
