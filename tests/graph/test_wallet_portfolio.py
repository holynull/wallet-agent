import json
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.domain.models import Asset, TokenBalance
from wallet_agent.graph.build import build_graph

EVM = "0x" + "1" * 40
BTC = "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"


class PortfolioModel:
    def __init__(self, arguments=None, tool="portfolio_snapshot"):
        self.arguments = arguments or {}
        self.tool = tool

    async def invoke_react(self, messages, tools):
        assert any("Current wallet App context" in str(item.content) for item in messages)
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content="这是已查询到的余额，请查看完整明细。")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "portfolio",
                    "name": self.tool,
                    "args": self.arguments,
                }
            ],
        )


class Wallet:
    chain_index_by_name = {"ETH": "1", "BASE": "8453", "BTC": "0", "TRON": "195"}

    async def get_token_balances(self, address, indexes):
        # Reject mixing address families or chains; external API boundary only.
        assert len(indexes) == 1
        index = indexes[0]
        if index == "0":
            assert address == BTC
            return [
                TokenBalance(
                    asset=Asset(chain="BTC", symbol="BTC", decimals=8),
                    amount=Decimal("0.01"),
                    amount_raw="1000000",
                    usd_value=Decimal("600"),
                )
            ]
        assert address == EVM
        if index == "8453":
            raise TimeoutError("provider unavailable")
        return [
            TokenBalance(
                asset=Asset(chain="ETH", symbol="ETH", decimals=18),
                amount=Decimal("0.00000001"),
                amount_raw="10000000000",
                usd_value=Decimal("0.000025"),
            )
        ]


async def run_portfolio(context, arguments=None, tool="portfolio_snapshot"):
    graph = build_graph(model=PortfolioModel(arguments, tool), wallet_provider=Wallet())
    result = await graph.ainvoke(
        {"request": {"message": "我的余额"}, "wallet_context": context},
        config={"configurable": {"thread_id": "portfolio-test"}},
    )
    observation = next(item for item in result["messages"] if isinstance(item, ToolMessage))
    return json.loads(observation.content), result


@pytest.mark.asyncio
async def test_app_portfolio_queries_btc_and_keeps_other_networks_when_base_fails():
    data, result = await run_portfolio(
        {
            "address": EVM,
            "chain": "ETH",
            "supported_networks": ["ETH", "BASE", "BTC", "TRON"],
            "wallet_accounts": [
                {"chain": "ETH", "address": EVM},
                {"chain": "BASE", "address": EVM},
                {"chain": "BTC", "address": BTC},
            ],
        }
    )
    portfolio = data["portfolio"]
    assert data["ok"] is True
    assert portfolio["complete"] is False
    assert portfolio["total_usd_value"] == "600.000025"
    assert [item["asset"]["chain"] for item in portfolio["assets"]] == ["BTC", "ETH"]
    assert {item["chain"]: item["status"] for item in portfolio["network_status"]} == {
        "ETH": "success",
        "BASE": "failed",
        "BTC": "success",
        "TRON": "missing_address",
    }
    assert result["response"]["portfolio"]["complete"] is False


@pytest.mark.asyncio
async def test_explicit_btc_scope_uses_btc_account_instead_of_connected_evm_address():
    data, _ = await run_portfolio(
        {
            "address": EVM,
            "chain": "ETH",
            "supported_networks": ["ETH", "BTC"],
            "wallet_accounts": [{"chain": "ETH", "address": EVM}, {"chain": "BTC", "address": BTC}],
        },
        {"chain": "BTC", "symbol": "BTC"},
    )
    assert data["portfolio"]["complete"] is True
    assert data["portfolio"]["asset_count"] == 1
    assert data["portfolio"]["total_usd_value"] == "600"


@pytest.mark.asyncio
async def test_wallet_balance_without_chain_uses_all_app_networks():
    data, _ = await run_portfolio(
        {
            "address": EVM,
            "chain": "ETH",
            "supported_networks": ["ETH", "BTC"],
            "wallet_accounts": [{"chain": "ETH", "address": EVM}, {"chain": "BTC", "address": BTC}],
        },
        tool="wallet_balance",
    )
    assert {item["chain"] for item in data["portfolio"]["network_status"]} == {"ETH", "BTC"}


@pytest.mark.asyncio
async def test_explicit_unsupported_app_network_does_not_query_provider():
    data, _ = await run_portfolio(
        {
            "address": EVM,
            "supported_networks": ["ETH"],
            "wallet_accounts": [{"chain": "ETH", "address": EVM}],
        },
        {"chain": "BASE"},
    )
    assert data["portfolio"]["network_status"][0]["status"] == "unsupported"
    assert data["portfolio"]["assets"] == []


@pytest.mark.asyncio
async def test_btc_duplicate_addresses_do_not_double_count_and_invalid_account_is_partial():
    data, _ = await run_portfolio(
        {
            "supported_networks": ["BTC"],
            "wallet_accounts": [
                {"chain": "BTC", "address": BTC},
                {"chain": "BTC", "address": BTC},
                {"chain": "BTC", "address": EVM},
            ],
        }
    )
    assert data["portfolio"]["total_usd_value"] == "600"
    assert data["portfolio"]["network_status"][0]["status"] == "partial"
    assert data["portfolio"]["complete"] is False


@pytest.mark.asyncio
async def test_asset_only_scope_filters_all_networks_without_restricting_to_current_chain():
    data, _ = await run_portfolio(
        {
            "address": EVM,
            "chain": "ETH",
            "supported_networks": ["ETH", "BTC"],
            "wallet_accounts": [{"chain": "ETH", "address": EVM}, {"chain": "BTC", "address": BTC}],
        },
        {"symbol": "BTC"},
    )
    assert data["portfolio"]["chains"] == ["ETH", "BTC"]
    assert data["portfolio"]["asset_count"] == 1
    assert data["portfolio"]["assets"][0]["asset"]["symbol"] == "BTC"


@pytest.mark.asyncio
async def test_timeout_preserves_successful_network_and_empty_context_does_not_reuse_address():
    import asyncio

    from wallet_agent.portfolio import query_portfolio

    class SlowWallet(Wallet):
        async def get_token_balances(self, address, indexes):
            if indexes == ["8453"]:
                await asyncio.sleep(1)
            return await super().get_token_balances(address, indexes)

    data = await query_portfolio(
        SlowWallet(), {"address": EVM, "supported_networks": ["ETH", "BASE"]}, {}, timeout=0.01
    )
    assert data["portfolio"]["asset_count"] == 1
    assert data["portfolio"]["network_status"][1]["accounts"][0]["code"] == "BALANCE_QUERY_TIMEOUT"
    empty = await query_portfolio(
        Wallet(), {"address": EVM, "supported_networks": [], "wallet_accounts": []}, {}
    )
    assert empty["portfolio"]["assets"] == []
    assert empty["portfolio"]["complete"] is False


@pytest.mark.parametrize(
    "address,valid",
    [
        (BTC, True),
        ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", True),
        ("BC1SW50QGDZ25J", True),
        ("bc1zw508d6qejxtdg4y5r3zarvaryvaxxpcs", True),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080", False),
        (BTC[:-1] + "X", False),
        (EVM, False),
    ],
)
def test_bitcoin_mainnet_checksums(address, valid):
    from wallet_agent.portfolio import valid_btc_address

    assert valid_btc_address(address) is valid


@pytest.mark.asyncio
async def test_multiple_distinct_btc_accounts_sum_and_ignore_total_value_api():
    from wallet_agent.portfolio import query_portfolio

    second = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"

    class MultiBtcWallet(Wallet):
        async def get_token_balances(self, address, indexes):
            assert address in (BTC, second)
            return await super().get_token_balances(BTC, indexes)

        async def get_total_value(self, *args):
            raise AssertionError("Another dataset must not determine the displayed total")

    data = await query_portfolio(
        MultiBtcWallet(),
        {
            "supported_networks": ["BTC"],
            "wallet_accounts": [
                {"chain": "BTC", "address": BTC},
                {"chain": "BTC", "address": second},
            ],
        },
        {},
    )
    assert data["portfolio"]["total_usd_value"] == "1200"
    assert data["portfolio"]["assets"][0]["amount"] == "0.02"
    assert data["portfolio"]["assets"][0]["amount_raw"] == "2000000"
