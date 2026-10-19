from decimal import Decimal

import pytest

from wallet_agent.domain.models import Asset, TransactionContext
from wallet_agent.okx.errors import OkxClientError
from wallet_agent.okx.wallet import OkxWalletAdapter, OkxWalletError

ADDRESS = "0x" + "1" * 40
TO = "0x" + "2" * 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain,symbol,index,amount,raw",
    [
        ("POLYGON", "POL", "137", "64.059133743331460466", "64059133743331460466"),
        ("BTC", "BTC", "0", "0.00000123", "123"),
    ],
)
async def test_native_pol_and_btc_balances_resolve_precision_without_metadata(
    chain, symbol, index, amount, raw
):
    client = FakeClient(
        {
            "/api/v6/dex/balance/all-token-balances-by-address": {
                "code": "0",
                "data": [
                    {
                        "tokenAssets": [
                            {
                                "chainIndex": index,
                                "symbol": symbol,
                                "balance": amount,
                                "rawBalance": raw,
                                "tokenContractAddress": "",
                                "tokenPrice": "1",
                            }
                        ]
                    }
                ],
            }
        }
    )
    balances = await OkxWalletAdapter(client, {chain: index}).get_token_balances(ADDRESS, [index])
    assert balances[0].amount_raw == raw
    assert balances[0].amount == Decimal(amount)


@pytest.mark.asyncio
async def test_token_balance_carries_okx_logo_url():
    client = FakeClient(
        {
            "/api/v6/dex/balance/all-token-balances-by-address": {
                "code": "0",
                "data": [
                    {
                        "tokenAssets": [
                            {
                                "chainIndex": "8453",
                                "symbol": "USDC",
                                "balance": "1.5",
                                "rawBalance": "1500000",
                                "tokenContractAddress": "0x" + "a" * 40,
                                "logoUrl": "https://static.okx.com/usdc.png",
                            }
                        ]
                    }
                ],
            },
            "/api/v6/dex/aggregator/all-tokens": {
                "code": "0",
                "data": [
                    {
                        "tokenContractAddress": "0x" + "a" * 40,
                        "tokenSymbol": "USDC",
                        "decimals": "6",
                    }
                ],
            },
        }
    )
    balances = await OkxWalletAdapter(client, {"BASE": "8453"}).get_token_balances(
        ADDRESS, ["8453"]
    )
    assert balances[0].asset.logo_url == "https://static.okx.com/usdc.png"


@pytest.mark.asyncio
async def test_token_balance_tolerates_missing_logo():
    client = FakeClient(
        {
            "/api/v6/dex/balance/all-token-balances-by-address": {
                "code": "0",
                "data": [
                    {
                        "tokenAssets": [
                            {
                                "chainIndex": "1",
                                "symbol": "ETH",
                                "balance": "0.1",
                                "rawBalance": "100000000000000000",
                                "tokenContractAddress": "",
                            }
                        ]
                    }
                ],
            }
        }
    )
    balances = await OkxWalletAdapter(client, {"ETH": "1"}).get_token_balances(ADDRESS, ["1"])
    assert balances[0].asset.logo_url is None


class FakeClient:
    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []

    async def request(self, method, path, *, query=None, body=None):
        self.calls.append((method, path, query, body))
        if self.error:
            raise self.error
        return self.responses[path]


@pytest.mark.asyncio
async def test_partial_snapshot_keeps_valid_tokens_and_strict_api_still_rejects_bad_record():
    client = FakeClient(
        {
            "/api/v6/dex/balance/all-token-balances-by-address": {
                "code": "0",
                "data": [
                    {
                        "tokenAssets": [
                            {
                                "chainIndex": "137",
                                "symbol": "POL",
                                "balance": "1",
                                "rawBalance": "1000000000000000000",
                                "tokenPrice": "1",
                            },
                            {
                                "chainIndex": "137",
                                "symbol": "BROKEN",
                                "tokenContractAddress": "0x" + "2" * 40,
                                "decimals": "6",
                                "balance": "1",
                                "rawBalance": "abc",
                            },
                        ]
                    }
                ],
            }
        }
    )
    adapter = OkxWalletAdapter(client, {"POLYGON": "137"})
    snapshot = await adapter.get_balance_snapshot(ADDRESS, ["137"])
    assert len(snapshot["balances"]) == 1
    assert snapshot["errors"] == [
        {"chain": "POLYGON", "symbol": "BROKEN", "code": "OKX_MALFORMED_RESPONSE"}
    ]
    with pytest.raises(OkxWalletError):
        await adapter.get_token_balances(ADDRESS, ["137"])


@pytest.mark.asyncio
async def test_btc_human_balance_without_raw_balance_uses_exact_satoshis():
    client = FakeClient(
        {
            "/api/v6/dex/balance/all-token-balances-by-address": {
                "code": "0",
                "data": [
                    {
                        "tokenAssets": [
                            {
                                "chainIndex": "0",
                                "symbol": "BTC",
                                "balance": "0.00000123",
                                "rawBalance": "",
                                "tokenPrice": "60000",
                            }
                        ]
                    }
                ],
            }
        }
    )
    balances = await OkxWalletAdapter(client, {"BTC": "0"}).get_token_balances(ADDRESS, ["0"])
    assert balances[0].amount_raw == "123"
    assert balances[0].usd_value == Decimal("0.0738")


@pytest.mark.asyncio
async def test_total_value_maps_chain_names_and_normalizes_decimal():
    client = FakeClient(
        {
            "/api/v6/dex/balance/total-value-by-address": {
                "code": "0",
                "data": [{"totalValue": "123.45"}],
            }
        }
    )
    adapter = OkxWalletAdapter(client, {"ETH": "1", "BSC": "56"})

    result = await adapter.get_total_value(ADDRESS, ["ETH", "BSC"])

    assert result.total_value == Decimal("123.45")
    assert client.calls[0][2] == {
        "address": ADDRESS,
        "chains": "1,56",
        "assetType": "0",
        "excludeRiskToken": True,
    }


@pytest.mark.asyncio
async def test_token_balances_construct_native_and_token_assets_with_risk_flags():
    client = FakeClient(
        {
            "/api/v6/dex/balance/all-token-balances-by-address": {
                "code": "0",
                "data": [
                    {
                        "tokenAssets": [
                            {
                                "chainIndex": "1",
                                "symbol": "ETH",
                                "tokenContractAddress": "",
                                "balance": "1.25",
                                "rawBalance": "1250000000000000000",
                                "tokenPrice": "2500.5",
                                "isRiskToken": False,
                            },
                            {
                                "chainIndex": "1",
                                "symbol": "USDC",
                                "tokenContractAddress": "0x" + "a" * 40,
                                "decimals": "6",
                                "balance": "12.5",
                                "rawBalance": "12500000",
                                "tokenPrice": "1",
                                "isRiskToken": True,
                            },
                        ],
                    }
                ],
            }
        }
    )
    adapter = OkxWalletAdapter(client, {"ETH": "1"})

    balances = await adapter.get_token_balances(ADDRESS)

    assert len(balances) == 2
    assert balances[0].asset.address is None
    assert balances[0].asset.chain == "ETH"
    assert balances[0].usd_value == Decimal("3125.625")
    assert balances[1].asset.address == "0x" + "a" * 40
    assert balances[1].is_risk_token is True
    assert client.calls[0][2]["chains"] == "1"
    assert client.calls[0][2]["excludeRiskToken"] == "0"


@pytest.mark.asyncio
async def test_token_balances_fill_missing_decimals_from_okx_token_metadata():
    token_address = "0x" + "a" * 40
    client = FakeClient(
        {
            "/api/v6/dex/balance/all-token-balances-by-address": {
                "code": "0",
                "data": [
                    {
                        "tokenAssets": [
                            {
                                "chainIndex": "1",
                                "symbol": "USDC",
                                "tokenContractAddress": token_address,
                                "balance": "12.5",
                                "rawBalance": "12500000",
                                "tokenPrice": "1",
                                "isRiskToken": False,
                            }
                        ]
                    }
                ],
            },
            "/api/v6/dex/aggregator/all-tokens": {
                "code": "0",
                "data": [
                    {
                        "tokenContractAddress": token_address,
                        "tokenSymbol": "USDC",
                        "decimals": "6",
                    }
                ],
            },
        }
    )
    adapter = OkxWalletAdapter(client, {"ETH": "1"})

    balances = await adapter.get_token_balances(ADDRESS, ["ETH"])

    assert balances[0].asset.decimals == 6
    assert client.calls[1] == (
        "GET",
        "/api/v6/dex/aggregator/all-tokens",
        {"chainIndex": "1"},
        None,
    )


@pytest.mark.asyncio
async def test_token_balances_infer_missing_decimals_only_from_exact_raw_amount():
    token_address = "0x" + "a" * 40
    client = FakeClient(
        {
            "/api/v6/dex/balance/all-token-balances-by-address": {
                "code": "0",
                "data": [
                    {
                        "tokenAssets": [
                            {
                                "chainIndex": "1",
                                "symbol": "USDC",
                                "tokenContractAddress": token_address,
                                "balance": "12.5",
                                "rawBalance": "12500000",
                                "isRiskToken": False,
                            }
                        ]
                    }
                ],
            },
            "/api/v6/dex/aggregator/all-tokens": {"code": "0", "data": []},
        }
    )
    adapter = OkxWalletAdapter(client, {"ETH": "1"})

    balances = await adapter.get_token_balances(ADDRESS, ["ETH"])

    assert balances[0].asset.decimals == 6


@pytest.mark.asyncio
async def test_pretransaction_calls_use_exact_documented_body_fields():
    client = FakeClient(
        {
            "/api/v6/dex/pre-transaction/gas-limit": {
                "code": "0",
                "data": [{"gasLimit": "21000"}],
            },
            "/api/v6/dex/pre-transaction/simulate": {
                "code": "0",
                "data": [{"gasUsed": "20500", "failReason": ""}],
            },
        }
    )
    adapter = OkxWalletAdapter(client, {"ETH": "1"})
    transaction = TransactionContext(
        chain="ETH",
        from_address=ADDRESS,
        to_address=TO,
        native_amount="100",
        calldata="0xabcdef",
    )

    estimate = await adapter.estimate_gas_limit(transaction)
    simulation = await adapter.simulate_transaction(transaction)

    expected = {
        "chainIndex": "1",
        "fromAddress": ADDRESS,
        "toAddress": TO,
        "txAmount": "100",
        "extJson": {"inputData": "0xabcdef"},
    }
    assert client.calls[0][3] == expected
    assert client.calls[1][3] == expected
    assert estimate.gas_limit == "21000"
    assert simulation.success is True
    assert simulation.gas_used == "20500"


@pytest.mark.asyncio
async def test_simulation_fail_reason_becomes_unsuccessful_normalized_evidence():
    client = FakeClient(
        {
            "/api/v6/dex/pre-transaction/simulate": {
                "code": "0",
                "data": [{"gasUsed": "21000", "failReason": "insufficient funds"}],
            }
        }
    )
    adapter = OkxWalletAdapter(client, {"ETH": "1"})
    transaction = TransactionContext(
        chain="ETH",
        from_address=ADDRESS,
        to_address=TO,
        native_amount="0",
        calldata="0x",
    )

    result = await adapter.simulate_transaction(transaction)

    assert result.success is False
    assert result.failure_reason == "insufficient funds"


@pytest.mark.asyncio
async def test_erc20_balance_requires_documented_decimals_and_raw_balance():
    client = FakeClient(
        {
            "/api/v6/dex/balance/all-token-balances-by-address": {
                "code": "0",
                "data": [
                    {
                        "tokenAssets": [
                            {
                                "chainIndex": "1",
                                "symbol": "USDC",
                                "tokenContractAddress": "0x" + "a" * 40,
                                "balance": "12.5",
                                "tokenPrice": "1",
                                "isRiskToken": False,
                            }
                        ]
                    }
                ],
            },
            "/api/v6/dex/aggregator/all-tokens": {"code": "0", "data": []},
        }
    )
    adapter = OkxWalletAdapter(client, {"ETH": "1"})

    with pytest.raises(OkxWalletError) as raised:
        await adapter.get_token_balances(ADDRESS, ["ETH"])

    assert raised.value.code == "OKX_MALFORMED_RESPONSE"


@pytest.mark.asyncio
async def test_non_finite_provider_numbers_are_stable_malformed_errors():
    client = FakeClient(
        {
            "/api/v6/dex/balance/total-value-by-address": {
                "code": "0",
                "data": [{"totalValue": "NaN"}],
            }
        }
    )
    adapter = OkxWalletAdapter(client, {"ETH": "1"})

    with pytest.raises(OkxWalletError) as raised:
        await adapter.get_total_value(ADDRESS, ["ETH"])

    assert raised.value.code == "OKX_MALFORMED_RESPONSE"


@pytest.mark.asyncio
async def test_unknown_chain_and_malformed_response_are_stable_agent_errors():
    adapter = OkxWalletAdapter(FakeClient(), {"ETH": "1"})
    with pytest.raises(OkxWalletError) as unknown:
        await adapter.get_total_value(ADDRESS, ["UNKNOWN"])
    assert unknown.value.code == "OKX_CHAIN_UNSUPPORTED"
    assert unknown.value.to_agent_error().code == "OKX_CHAIN_UNSUPPORTED"

    malformed_client = FakeClient(
        {"/api/v6/dex/balance/total-value-by-address": {"code": "0", "data": []}}
    )
    adapter = OkxWalletAdapter(malformed_client, {"ETH": "1"})
    with pytest.raises(OkxWalletError) as malformed:
        await adapter.get_total_value(ADDRESS, ["ETH"])
    assert malformed.value.code == "OKX_MALFORMED_RESPONSE"


@pytest.mark.asyncio
async def test_provider_errors_are_sanitized_and_not_retried_by_adapter():
    client = FakeClient(error=OkxClientError("50101", "OKX request failed"))
    adapter = OkxWalletAdapter(client, {"ETH": "1"})

    with pytest.raises(OkxWalletError) as raised:
        await adapter.get_total_value(ADDRESS, ["ETH"])

    assert raised.value.code == "OKX_PROVIDER_ERROR"
    assert "50101" not in str(raised.value)
    assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_specific_balances_posts_selected_assets_in_batches():
    client = FakeClient(
        {
            "/api/v6/dex/balance/token-balances-by-address": {
                "code": "0",
                "data": [
                    {
                        "chainIndex": "1",
                        "tokenContractAddress": "0x" + "a" * 40,
                        "balance": "2",
                        "rawBalance": "2000000",
                    }
                ],
            }
        }
    )
    adapter = OkxWalletAdapter(client, {"ETH": "1"})
    asset = Asset(chain="ETH", symbol="USDC", decimals=6, address="0x" + "a" * 40)

    balances = await adapter.get_specific_balances(ADDRESS, [asset])

    assert balances[0].asset.symbol == "USDC"
    assert client.calls[0] == (
        "POST",
        "/api/v6/dex/balance/token-balances-by-address",
        None,
        {
            "address": ADDRESS,
            "tokenContractAddresses": [
                {"chainIndex": "1", "tokenContractAddress": "0x" + "a" * 40}
            ],
            "excludeRiskToken": "0",
        },
    )
