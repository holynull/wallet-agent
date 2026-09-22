import os

import pytest

from wallet_agent.okx import OkxSignedClient

from .test_live_contract_smoke import _live_config


def _live_okx_configuration() -> dict[str, str] | None:
    config = _live_config(
        "OKX",
        (
            "API_KEY",
            "SECRET_KEY",
            "PASSPHRASE",
            "PROJECT_ID",
            "INTEGRATION_ADDRESS",
            "INTEGRATION_TOKEN_ADDRESS",
        ),
    )
    if config is None:
        return None
    return {f"OKX_{key}": value for key, value in config.items()}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_okx_read_only_wallet_market_and_pretransaction_contracts():
    config = _live_okx_configuration()
    if config is None:
        pytest.skip(
            "set OKX_INTEGRATION=1, OKX credentials, address, and token address to run"
        )
    client = OkxSignedClient(
        os.getenv("OKX_BASE_URL", "https://web3.okx.com"),
        api_key=config["OKX_API_KEY"],
        secret_key=config["OKX_SECRET_KEY"],
        passphrase=config["OKX_PASSPHRASE"],
        project_id=config["OKX_PROJECT_ID"],
        max_attempts=1,
    )
    address = config["OKX_INTEGRATION_ADDRESS"]
    token_address = config["OKX_INTEGRATION_TOKEN_ADDRESS"].lower()
    try:
        responses = [
            await client.request(
                "GET",
                "/api/v6/dex/balance/total-value-by-address",
                query={"address": address, "chains": "1", "assetType": "0"},
            ),
            await client.request(
                "GET",
                "/api/v6/dex/balance/all-token-balances-by-address",
                query={"address": address, "chains": "1", "excludeRiskToken": "0"},
            ),
            await client.request(
                "POST",
                "/api/v6/dex/market/price",
                body=[{"chainIndex": "1", "tokenContractAddress": token_address}],
            ),
            await client.request(
                "POST",
                "/api/v6/dex/pre-transaction/gas-limit",
                body={
                    "chainIndex": "1",
                    "fromAddress": address,
                    "toAddress": address,
                    "txAmount": "0",
                    "extJson": {"inputData": "0x"},
                },
            ),
            await client.request(
                "POST",
                "/api/v6/dex/pre-transaction/simulate",
                body={
                    "chainIndex": "1",
                    "fromAddress": address,
                    "toAddress": address,
                    "txAmount": "0",
                    "extJson": {"inputData": "0x"},
                },
            ),
        ]
    finally:
        await client.aclose()

    assert all(response.get("code") == "0" for response in responses)
