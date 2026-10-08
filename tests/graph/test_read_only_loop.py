import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.graph.read_only import (
    READ_ONLY_MAX_PARALLEL_TOOLS,
    READ_ONLY_TOOL_NAMES,
    ReadOnlyToolCall,
    read_tool_signature,
    route_read_only,
    run_read_only_round,
)
from wallet_agent.graph.routes import route_after_intent


def test_registry_contains_only_read_capabilities():
    assert READ_ONLY_TOOL_NAMES == frozenset(
        {
            "wallet_balance",
            "price_lookup",
            "gas_estimate",
            "asset_discovery",
            "transaction_status",
            "market_details",
            "price_history",
            "portfolio_snapshot",
        }
    )


def test_read_tool_signature_is_stable_for_equivalent_arguments():
    assert read_tool_signature(
        "price_lookup", {"symbol": "ETH", "chain": "BASE"}
    ) == read_tool_signature(
        "price_lookup", {"chain": "BASE", "symbol": "ETH"}
    )


def test_native_read_batch_limit_is_bounded():
    assert READ_ONLY_MAX_PARALLEL_TOOLS == 4


def test_route_stops_after_two_rounds():
    assert route_read_only({"read_loop_count": 0, "read_tool_call": None}) == "agent"
    assert route_read_only(
        {"read_loop_count": 0, "read_tool_call": {"name": "price_lookup"}}
    ) == "tool"
    assert route_read_only(
        {"read_loop_count": 4, "read_tool_call": {"name": "price_lookup"}}
    ) == "response"


@pytest.mark.parametrize("intent", ["market_details", "price_history", "portfolio_query"])
def test_extended_read_intents_use_the_native_read_only_route(intent):
    assert route_after_intent({"intent": intent, "read_only_enabled": True}) == "read_only"


@pytest.mark.asyncio
async def test_native_read_tool_call_round_executes_tool_and_returns_to_model():
    from decimal import Decimal

    from wallet_agent.domain.models import TokenPrice
    from wallet_agent.graph.build import build_graph

    class PriceProvider:
        async def get_prices(self, assets):
            return [
                TokenPrice(
                    asset=assets[0],
                    usd_price=Decimal("1800"),
                    provider="okx",
                )
            ]

    class NativeReadModel:
        read_agent_enabled = True

        def __init__(self):
            self.calls = []

        async def classify(self, _request):
            return {"intent": "price_query"}

        async def extract(self, _task_kind, _request):
            return {"symbol": "ETH", "chain": "ETH", "decimals": 18}

        async def invoke_read_tools(self, messages, _tools):
            self.calls.append(messages)
            if not any(isinstance(item, ToolMessage) for item in messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-price-1",
                            "name": "price_lookup",
                            "args": {"symbol": "ETH", "chain": "ETH", "decimals": 18},
                        }
                    ],
                )
            return AIMessage(content="无需继续查询")

    model = NativeReadModel()
    result = await build_graph(model=model, price_provider=PriceProvider()).ainvoke(
        {"request": {"message": "ETH 现在多少钱"}},
        config={"configurable": {"thread_id": "native-read-only"}},
    )

    assert result["response"]["kind"] == "price_query"
    assert result["response"]["prices"][0]["usd_price"] == "1800"
    assert result["read_tool_calls"][0]["call_id"] == "call-price-1"
    assert result["read_tool_calls"][0]["status"] == "completed"
    assert len(model.calls) == 2
    assert any(isinstance(item, ToolMessage) for item in model.calls[1])
    assert route_read_only(
        {"read_loop_count": 1, "read_tool_call": None, "read_only_complete": True}
    ) == "response"


@pytest.mark.asyncio
async def test_native_tool_arguments_are_forwarded_to_the_tool_node():
    """Native tool wrappers must preserve arguments selected by DeepSeek."""
    from decimal import Decimal

    from wallet_agent.domain.models import TokenPrice
    from wallet_agent.graph.build import build_graph

    class PriceProvider:
        def __init__(self):
            self.assets = []

        async def get_prices(self, assets):
            self.assets.extend(assets)
            return [TokenPrice(asset=assets[0], usd_price=Decimal("765"), provider="okx")]

    class NativeReadModel:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "price_query"}

        async def extract(self, _task_kind, _request):
            # The model tool call below deliberately corrects this stale extraction.
            return {"symbol": "ETH", "chain": "ETH", "decimals": 18}

        async def invoke_read_tools(self, messages, _tools):
            if not any(isinstance(item, ToolMessage) for item in messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-bnb-price",
                            "name": "price_lookup",
                            "args": {"symbol": "BNB", "chain": "BSC"},
                        }
                    ],
                )
            return AIMessage(content="")

    provider = PriceProvider()
    result = await build_graph(model=NativeReadModel(), price_provider=provider).ainvoke(
        {"request": {"message": "bnb 现在价值多少u"}},
        config={"configurable": {"thread_id": "native-tool-args-forwarded"}},
    )

    assert result["response"]["kind"] == "price_query"
    assert provider.assets[0].symbol == "BNB"
    assert provider.assets[0].chain == "BSC"


@pytest.mark.asyncio
async def test_parallel_balance_and_price_calls_produce_asset_value_answer():
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance, TokenPrice
    from wallet_agent.graph.build import build_graph

    bnb = Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18, address=None)

    class WalletProvider:
        async def get_token_balances(self, _address, _indexes):
            return [
                TokenBalance(
                    asset=bnb,
                    amount=Decimal("0.004818285427772361"),
                    amount_raw="4818285427772361",
                    provider="okx",
                )
            ]

    class PriceProvider:
        async def get_prices(self, assets):
            return [TokenPrice(asset=assets[0], usd_price=Decimal("765"), provider="okx")]

    class NativeReadModel:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "price_query"}

        async def extract(self, _task_kind, _request):
            return {"symbol": "BNB", "chain": "BSC", "decimals": 18}

        async def invoke_read_tools(self, messages, _tools):
            if not any(isinstance(item, ToolMessage) for item in messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-balance",
                            "name": "wallet_balance",
                            "args": {"chain": "BSC", "address": "0x" + "1" * 40},
                        },
                        {
                            "id": "call-price",
                            "name": "price_lookup",
                            "args": {"chain": "BSC", "symbol": "BNB"},
                        },
                    ],
                )
            return AIMessage(content="")

    result = await build_graph(
        model=NativeReadModel(),
        wallet_provider=WalletProvider(),
        price_provider=PriceProvider(),
    ).ainvoke(
        {
            "request": {
                "message": "我的 BNB 价值多少 U",
                "address": "0x" + "1" * 40,
                "chain": "BSC",
            }
        },
        config={"configurable": {"thread_id": "parallel-asset-value"}},
    )

    assert result["response"]["kind"] == "asset_value"
    assert result["response"]["asset"]["symbol"] == "BNB"
    assert result["response"]["usd_value"] == "3.685988352245856165"
    assert "请指定要查询的资产" not in result["response"]["message"]


@pytest.mark.asyncio
async def test_asset_value_keeps_balance_fact_when_price_provider_fails():
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance
    from wallet_agent.graph.build import build_graph

    bnb = Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18, address=None)
    usdt = Asset(
        chain="BSC", chain_id=56, symbol="USDT", decimals=18, address="0x" + "2" * 40
    )

    class WalletProvider:
        async def get_token_balances(self, _address, _indexes):
            return [
                TokenBalance(
                    asset=bnb, amount=Decimal("0.0048"), amount_raw="4800", provider="okx"
                ),
                TokenBalance(asset=usdt, amount=Decimal("10"), amount_raw="10", provider="okx"),
            ]

    class PriceProvider:
        async def get_prices(self, _assets):
            raise RuntimeError("temporary price outage")

    class NativeReadModel:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "price_query"}

        async def extract(self, _task_kind, _request):
            return {"symbol": "BNB", "chain": "BSC", "decimals": 18}

        async def invoke_read_tools(self, messages, _tools):
            if not any(isinstance(item, ToolMessage) for item in messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-balance",
                            "name": "wallet_balance",
                            "args": {"chain": "BSC", "address": "0x" + "1" * 40},
                        },
                        {
                            "id": "call-price",
                            "name": "price_lookup",
                            "args": {"chain": "BSC", "symbol": "BNB"},
                        },
                    ],
                )
            return AIMessage(content="")

    result = await build_graph(
        model=NativeReadModel(),
        wallet_provider=WalletProvider(),
        price_provider=PriceProvider(),
    ).ainvoke(
        {
            "request": {
                "message": "我的 BNB 价值多少 U",
                "address": "0x" + "1" * 40,
                "chain": "BSC",
            }
        },
        config={"configurable": {"thread_id": "asset-value-price-failure"}},
    )

    assert result["response"]["kind"] == "asset_value"
    assert result["response"]["balance"]["asset"]["symbol"] == "BNB"
    assert "暂时无法计算美元价值" in result["response"]["message"]
    assert "请指定要查询的资产" not in result["response"]["message"]


def test_asset_value_followup_is_recognized_from_explicit_value_language():
    from wallet_agent.graph.nodes import _looks_like_asset_value_query

    assert _looks_like_asset_value_query("我说我的bnb 价值多少 u?") is True
    assert _looks_like_asset_value_query("我的 bnb 余额是多少") is False


def test_explicit_balance_chain_does_not_treat_token_symbols_as_networks():
    from wallet_agent.graph.nodes import _explicit_balance_chain

    supported = {"ETH", "SOLANA", "BASE"}
    assert _explicit_balance_chain("查询其他链上的 WETH 余额", supported) is None
    assert _explicit_balance_chain("查询其他链上的 SOLV 余额", supported) is None
    assert _explicit_balance_chain("查询其他链 Base 的余额", supported) == "BASE"


@pytest.mark.asyncio
async def test_preflight_balance_followup_explains_the_previous_failure():
    from wallet_agent.graph.build import build_graph

    class Model:
        async def classify(self, _request):
            raise AssertionError("the preflight follow-up should be deterministic")

    result = await build_graph(model=Model()).ainvoke(
        {
            "conversation_history": [
                {
                    "role": "assistant",
                    "content": "交易预检查未通过",
                    "artifacts": {
                        "response": {
                            "kind": "error",
                            "errors": [
                                {
                                    "code": "TRANSACTION_PREFLIGHT_FAILED",
                                    "details": {
                                        "preflight": {
                                            "chain": "BSC",
                                            "checks": [
                                                {
                                                    "status": "failed",
                                                    "code": "INSUFFICIENT_BALANCE",
                                                    "message": (
                                                        "原生币余额不足以支付交易金额和网络手续费。"
                                                    ),
                                                }
                                            ],
                                        }
                                    },
                                }
                            ],
                        }
                    },
                }
            ],
            "request": {"message": "是我的余额不足吗"},
        },
        config={"configurable": {"thread_id": "preflight-balance-followup"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert "余额不足" in result["response"]["message"]
    assert result["response"]["preflight"]["chain"] == "BSC"


@pytest.mark.asyncio
async def test_repeated_native_tool_call_reuses_verified_fact_without_requerying_provider():
    from decimal import Decimal

    from wallet_agent.domain.models import TokenPrice
    from wallet_agent.graph.build import build_graph

    class PriceProvider:
        def __init__(self):
            self.calls = 0

        async def get_prices(self, assets):
            self.calls += 1
            return [TokenPrice(asset=assets[0], usd_price=Decimal("1800"), provider="okx")]

    class RepeatingReadModel:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "price_query"}

        async def extract(self, _task_kind, _request):
            return {"symbol": "ETH", "chain": "ETH", "decimals": 18}

        async def invoke_read_tools(self, messages, _tools):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"call-price-{len(messages)}",
                        "name": "price_lookup",
                        "args": {"symbol": "ETH", "chain": "ETH", "decimals": 18},
                    }
                ],
            )

    provider = PriceProvider()
    result = await build_graph(model=RepeatingReadModel(), price_provider=provider).ainvoke(
        {"request": {"message": "ETH 现在多少钱"}},
        config={"configurable": {"thread_id": "native-repeat-read-only"}},
    )

    assert result["response"]["kind"] == "price_query"
    assert provider.calls == 1
    assert len(result["read_tool_calls"]) == 2
    assert result["read_tool_calls"][0]["status"] == "completed"
    assert result["read_tool_calls"][1]["status"] == "reused"


@pytest.mark.asyncio
async def test_native_read_tool_batch_over_limit_returns_safe_error():
    from wallet_agent.graph.build import build_graph

    class TooManyCallsModel:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "price_query"}

        async def extract(self, _task_kind, _request):
            return {"symbol": "ETH", "chain": "ETH"}

        async def invoke_read_tools(self, _messages, _tools):
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": f"call-{index}", "name": "price_lookup", "args": {"symbol": "ETH"}}
                    for index in range(READ_ONLY_MAX_PARALLEL_TOOLS + 1)
                ],
            )

    result = await build_graph(model=TooManyCallsModel()).ainvoke(
        {"request": {"message": "ETH 现在多少钱"}},
        config={"configurable": {"thread_id": "native-over-limit-read-only"}},
    )

    assert result["response"]["kind"] == "error"
    assert result["response"]["errors"][0]["code"] == "READ_TOOL_BATCH_TOO_LARGE"


@pytest.mark.asyncio
async def test_run_read_only_round_records_verified_tool_observation():
    async def lookup(args):
        return {"symbol": args["symbol"], "usd_price": "1"}

    result = await run_read_only_round(
        {"read_loop_count": 0, "read_tool_calls": [], "read_facts": {}},
        ReadOnlyToolCall(name="price_lookup", arguments={"symbol": "USDC"}),
        {"price_lookup": lookup},
    )

    assert result["read_loop_count"] == 1
    assert result["read_facts"] == {"price_lookup": {"symbol": "USDC", "usd_price": "1"}}
    assert result["read_tool_calls"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_unknown_tool_is_rejected_without_execution():
    called = False

    async def unsafe(_args):
        nonlocal called
        called = True

    result = await run_read_only_round(
        {"read_loop_count": 0, "read_tool_calls": [], "read_facts": {}},
        ReadOnlyToolCall(name="transfer", arguments={}),
        {"transfer": unsafe},
    )

    assert called is False
    assert result["read_loop_count"] == 1
    assert result["read_tool_calls"][0]["status"] == "rejected"
    assert result["read_facts"]["error"]["code"] == "READ_TOOL_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_plan_read_is_optional_and_legacy_models_do_not_enter_loop():
    from wallet_agent.graph.read_only import read_only_decision

    class LegacyModel:
        pass

    assert await read_only_decision(LegacyModel(), {"message": "price"}) is None


@pytest.mark.asyncio
async def test_plan_read_rejects_write_tool_before_execution():
    from wallet_agent.graph.read_only import read_only_decision

    class Planner:
        async def plan_read(self, _request):
            return {"name": "transfer", "arguments": {"amount": "1"}}

    call = await read_only_decision(Planner(), {"message": "send"})

    assert call is None


@pytest.mark.asyncio
async def test_graph_uses_opt_in_read_planner_without_entering_write_path():
    from decimal import Decimal

    from wallet_agent.domain.models import TokenPrice
    from wallet_agent.graph.build import build_graph

    class PriceProvider:
        async def get_prices(self, assets):
            return [
                TokenPrice(
                    asset=assets[0],
                    usd_price=Decimal("1800"),
                    provider="okx",
                )
            ]

    class ReadPlanner:
        async def classify(self, _request):
            return {"intent": "price_query"}

        async def extract(self, _task_kind, _request):
            return {"symbol": "ETH", "chain": "ETH", "decimals": 18}

        def __init__(self):
            self.rounds = 0

        async def plan_read(self, request):
            self.rounds += 1
            assert request["read_loop_count"] == self.rounds - 1
            if self.rounds > 2:
                return None
            return {
                "name": "price_lookup",
                "arguments": {"symbol": "ETH", "chain": "ETH", "decimals": 18},
            }

    model = ReadPlanner()
    graph = build_graph(model=model, price_provider=PriceProvider())
    result = await graph.ainvoke(
        {
            "conversation_id": "read-only-safe",
            "user_id": "u1",
            "request": {"message": "ETH 现在多少钱"},
        },
        config={"configurable": {"thread_id": "read-only-safe"}},
    )

    assert result["response"]["kind"] == "price_query"
    assert result["response"]["prices"][0]["usd_price"] == "1800"
    assert result["read_tool_calls"][0]["name"] == "price_lookup"
    assert result["read_tool_calls"][0]["status"] == "completed"
    assert model.rounds == 3
    assert result["read_loop_count"] == 2
    assert result["read_only_enabled"] is True
    assert result["read_only_complete"] is True
    assert result.get("transfer_request") is None
    assert result.get("swap_request") is None


@pytest.mark.asyncio
async def test_price_lookup_infers_native_asset_metadata_from_symbol():
    from decimal import Decimal

    from wallet_agent.domain.models import TokenPrice
    from wallet_agent.graph.build import build_graph

    class PriceProvider:
        def __init__(self):
            self.assets = []

        async def get_prices(self, assets):
            self.assets.extend(assets)
            return [TokenPrice(asset=assets[0], usd_price=Decimal("1800"), provider="okx")]

    class Planner:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "price_query"}

        async def extract(self, _task_kind, _request):
            return {"symbol": "ETH"}

        async def plan_read(self, request):
            if request["read_loop_count"]:
                return None
            return {"name": "price_lookup", "arguments": {"symbol": "ETH"}}

    provider = PriceProvider()
    result = await build_graph(model=Planner(), price_provider=provider).ainvoke(
        {"request": {"message": "ETH 价值多少 u"}},
        config={"configurable": {"thread_id": "price-native-inference"}},
    )

    assert result["response"]["kind"] == "price_query"
    assert result["response"]["prices"][0]["usd_price"] == "1800"
    assert provider.assets[0].chain == "ETH"
    assert provider.assets[0].decimals == 18
    assert provider.assets[0].chain_id == 1


@pytest.mark.asyncio
async def test_price_lookup_requests_chain_for_ambiguous_token():
    from wallet_agent.graph.build import build_graph

    class Planner:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "price_query"}

        async def extract(self, _task_kind, _request):
            return {"symbol": "USDC"}

        async def plan_read(self, request):
            if request["read_loop_count"]:
                return None
            return {"name": "price_lookup", "arguments": {"symbol": "USDC"}}

    result = await build_graph(model=Planner()).ainvoke(
        {"request": {"message": "USDC 价值多少 u"}},
        config={"configurable": {"thread_id": "price-ambiguous-token"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert result["response"]["errors"][0]["code"] == "PRICE_CHAIN_REQUIRED"
    assert "Field required" not in result["response"]["message"]


@pytest.mark.asyncio
async def test_price_lookup_resolves_token_metadata_from_catalog():
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenPrice
    from wallet_agent.graph.build import build_graph

    class Catalog:
        async def list_assets(self, query):
            assert query.chain == "ETH"
            assert query.search == "USDC"
            return [
                Asset(
                    chain="ETH",
                    chain_id=1,
                    symbol="USDC",
                    decimals=6,
                    address="0x" + "1" * 40,
                )
            ]

    class PriceProvider:
        def __init__(self):
            self.asset = None

        async def get_prices(self, assets):
            self.asset = assets[0]
            return [TokenPrice(asset=self.asset, usd_price=Decimal("1"), provider="okx")]

    class Planner:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "price_query"}

        async def extract(self, _task_kind, _request):
            return {"symbol": "USDC", "chain": "ETH"}

        async def plan_read(self, request):
            if request["read_loop_count"]:
                return None
            return {"name": "price_lookup", "arguments": {"symbol": "USDC", "chain": "ETH"}}

    provider = PriceProvider()
    result = await build_graph(
        model=Planner(), providers={"catalog": Catalog()}, price_provider=provider
    ).ainvoke(
        {"request": {"message": "ETH 链 USDC 价格"}},
        config={"configurable": {"thread_id": "price-catalog-resolution"}},
    )

    assert result["response"]["kind"] == "price_query"
    assert provider.asset.address == "0x" + "1" * 40
    assert provider.asset.decimals == 6


@pytest.mark.asyncio
async def test_unsupported_wallet_chain_returns_recoverable_supported_chain_prompt():
    from wallet_agent.graph.build import build_graph

    class Wallet:
        chain_index_by_name = {"ETH": "1", "BASE": "8453"}

        async def get_token_balances(self, _address, _chain_indexes):
            raise AssertionError("unsupported chain must be rejected before provider call")

    class Planner:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "wallet_query"}

        async def plan_read(self, _request):
            return {
                "name": "wallet_balance",
                "arguments": {"chain": "BTC", "address": "0x" + "1" * 40},
            }

    result = await build_graph(model=Planner(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {"message": "查询 BTC 余额", "address": "0x" + "1" * 40, "chain": "ETH"},
            "wallet_context": {"address": "0x" + "1" * 40, "chain": "ETH"},
        },
        config={"configurable": {"thread_id": "unsupported-wallet-chain"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert result["response"]["errors"][0]["code"] == "OKX_CHAIN_UNSUPPORTED"
    assert result["response"]["errors"][0]["details"]["requested_chain"] == "BTC"
    assert result["response"]["supported_chains"] == ["BASE", "ETH"]
    assert "BTC" in result["response"]["message"]


@pytest.mark.asyncio
async def test_other_chain_balance_question_short_circuits_planner_and_asks_for_chain():
    from wallet_agent.graph.build import build_graph

    class Wallet:
        chain_index_by_name = {"ETH": "1", "BASE": "8453"}

    class Planner:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "wallet_query"}

        async def plan_read(self, _request):
            raise AssertionError("ambiguous other-chain request should not invoke the planner")

    result = await build_graph(model=Planner(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {"message": "其他链的余额呢", "address": "0x" + "1" * 40, "chain": "ETH"},
            "wallet_context": {"address": "0x" + "1" * 40, "chain": "ETH"},
        },
        config={"configurable": {"thread_id": "other-chain-balance"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert result["response"]["errors"][0]["code"] == "WALLET_CHAIN_REQUIRED"
    assert result["response"]["supported_chains"] == ["BASE", "ETH"]


@pytest.mark.asyncio
async def test_portfolio_snapshot_summarizes_wallet_balances_instead_of_zero_assets():
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance
    from wallet_agent.graph.build import build_graph

    class Wallet:
        chain_index_by_name = {"BSC": "56"}
        chain_name_by_index = {"56": "BSC"}

        async def get_token_balances(self, _address, chain_indexes):
            assert chain_indexes == ["56"]
            return [
                TokenBalance(
                    asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
                    amount=Decimal("0.5"),
                    amount_raw="500000000000000000",
                ),
                TokenBalance(
                    asset=Asset(
                        chain="BSC",
                        chain_id=56,
                        symbol="USDC",
                        decimals=18,
                        address="0x" + "2" * 40,
                    ),
                    amount=Decimal("12"),
                    amount_raw="12000000000000000000",
                ),
            ]

        async def get_total_value(self, _address, chain_indexes):
            assert chain_indexes == ["56"]
            return {"total_value": "20", "chain_indexes": chain_indexes}

    class Model:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "wallet_query"}

        async def invoke_read_tools(self, messages, _tools):
            if not any(isinstance(item, ToolMessage) for item in messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-portfolio",
                            "name": "portfolio_snapshot",
                            "args": {
                                "chain": "BSC",
                                "address": "0x" + "1" * 40,
                            },
                        }
                    ],
                )
            return AIMessage(content="")

    result = await build_graph(model=Model(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {
                "message": "我的 BSC 资产组合",
                "address": "0x" + "1" * 40,
                "chain": "BSC",
            },
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "BSC",
            },
        },
        config={"configurable": {"thread_id": "portfolio-summary-contract"}},
    )

    assert result["response"]["kind"] == "portfolio_query"
    assert "2 项资产" in result["response"]["message"]
    assert "BNB" in result["response"]["message"]
    assert "USDC" in result["response"]["message"]


@pytest.mark.asyncio
async def test_other_chain_portfolio_question_requires_chain_before_tool_call():
    from wallet_agent.graph.build import build_graph

    class Wallet:
        chain_index_by_name = {"ETH": "1", "BASE": "8453"}

    class Model:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "wallet_query"}

        async def invoke_read_tools(self, *_args):
            raise AssertionError("ambiguous other-chain portfolio must not call a tool")

    result = await build_graph(model=Model(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {
                "message": "我的其他链的全部余额呢？",
                "address": "0x" + "1" * 40,
                "chain": "ETH",
            },
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "ETH",
            },
        },
        config={"configurable": {"thread_id": "other-chain-portfolio"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert result["response"]["errors"][0]["code"] == "WALLET_CHAIN_REQUIRED"
    assert result["response"]["supported_chains"] == ["BASE", "ETH"]


@pytest.mark.asyncio
async def test_other_chain_portfolio_with_explicit_network_queries_that_network():
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance
    from wallet_agent.graph.build import build_graph

    class Wallet:
        chain_index_by_name = {"ETH": "1", "BASE": "8453"}
        chain_name_by_index = {"1": "ETH", "8453": "BASE"}

        async def get_token_balances(self, _address, chain_indexes):
            assert chain_indexes == ["8453"]
            return [
                TokenBalance(
                    asset=Asset(chain="BASE", chain_id=8453, symbol="ETH", decimals=18),
                    amount=Decimal("0.1"),
                    amount_raw="100000000000000000",
                )
            ]

    class Model:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "portfolio_query"}

        async def invoke_read_tools(self, messages, _tools):
            if not any(isinstance(item, ToolMessage) for item in messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-explicit-other-chain",
                            "name": "portfolio_snapshot",
                            "args": {
                                "chain": "BASE",
                                "address": "0x" + "1" * 40,
                            },
                        }
                    ],
                )
            return AIMessage(content="")

    result = await build_graph(model=Model(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {
                "message": "查询其他链 Base 的全部余额",
                "address": "0x" + "1" * 40,
                "chain": "ETH",
            },
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "ETH",
            },
        },
        config={"configurable": {"thread_id": "explicit-other-chain-portfolio"}},
    )

    assert result["response"]["kind"] == "portfolio_query"
    assert result["response"]["scope"]["resolved_chains"] == ["BASE"]


@pytest.mark.asyncio
async def test_all_chain_portfolio_queries_every_supported_network():
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance
    from wallet_agent.graph.build import build_graph

    class Wallet:
        chain_index_by_name = {"ETH": "1", "BASE": "8453"}
        chain_name_by_index = {"1": "ETH", "8453": "BASE"}

        async def get_token_balances(self, _address, chain_indexes):
            assert chain_indexes == ["1", "8453"]
            return [
                TokenBalance(
                    asset=Asset(chain="ETH", chain_id=1, symbol="ETH", decimals=18),
                    amount=Decimal("0.1"),
                    amount_raw="100000000000000000",
                ),
                TokenBalance(
                    asset=Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6),
                    amount=Decimal("5"),
                    amount_raw="5000000",
                ),
            ]

        async def get_total_value(self, _address, chain_indexes):
            assert chain_indexes == ["1", "8453"]
            return {"total_value": "305", "chain_indexes": chain_indexes}

    class Model:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "portfolio_query"}

        async def invoke_read_tools(self, messages, _tools):
            if not any(isinstance(item, ToolMessage) for item in messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-all-chains",
                            "name": "portfolio_snapshot",
                            "args": {"address": "0x" + "1" * 40},
                        }
                    ],
                )
            return AIMessage(content="")

    result = await build_graph(model=Model(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {
                "message": "查询我所有链的全部余额",
                "address": "0x" + "1" * 40,
                "chain": "ETH",
            },
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "ETH",
            },
        },
        config={"configurable": {"thread_id": "all-chain-portfolio"}},
    )

    assert result["response"]["scope"]["requested"] == "all"
    assert result["response"]["scope"]["resolved_chains"] == ["ETH", "BASE"]
    assert result["response"]["asset_count"] == 2
    assert "2 项资产" in result["response"]["message"]


@pytest.mark.asyncio
async def test_portfolio_rejects_model_message_that_contradicts_asset_count():
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance
    from wallet_agent.graph.build import build_graph
    from wallet_agent.models.contracts import AgentResponseDraft

    class Wallet:
        chain_index_by_name = {"BSC": "56"}
        chain_name_by_index = {"56": "BSC"}

        async def get_token_balances(self, _address, _indexes):
            return [
                TokenBalance(
                    asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
                    amount=Decimal("0.5"),
                    amount_raw="500000000000000000",
                )
            ]

    class Model:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "portfolio_query"}

        async def invoke_read_tools(self, messages, _tools):
            if not any(isinstance(item, ToolMessage) for item in messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-portfolio-contradiction",
                            "name": "portfolio_snapshot",
                            "args": {
                                "chain": "BSC",
                                "address": "0x" + "1" * 40,
                            },
                        }
                    ],
                )
            return AIMessage(content="")

        async def respond(self, _request, _facts):
            return AgentResponseDraft(
                language="zh",
                message="当前没有查到任何资产。",
                suggestions=[],
            )

    result = await build_graph(model=Model(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {
                "message": "我的 BSC 资产组合",
                "address": "0x" + "1" * 40,
                "chain": "BSC",
            },
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "BSC",
            },
        },
        config={"configurable": {"thread_id": "portfolio-fact-validation"}},
    )

    assert "没有查到任何资产" not in result["response"]["message"]
    assert "1 项资产" in result["response"]["message"]


@pytest.mark.asyncio
async def test_explicit_bnb_balance_returns_only_bnb_not_the_full_chain_portfolio():
    """An asset-scoped balance request must not leak sibling token balances."""
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance
    from wallet_agent.graph.build import build_graph

    class Wallet:
        chain_index_by_name = {"BSC": "56"}
        chain_name_by_index = {"56": "BSC"}

        async def get_token_balances(self, _address, chain_indexes):
            assert chain_indexes == ["56"]
            return [
                TokenBalance(
                    asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
                    amount=Decimal("0.5"),
                    amount_raw="500000000000000000",
                    usd_value=Decimal("150"),
                ),
                TokenBalance(
                    asset=Asset(
                        chain="BSC",
                        chain_id=56,
                        symbol="USDC",
                        decimals=6,
                        address="0x" + "2" * 40,
                    ),
                    amount=Decimal("12"),
                    amount_raw="12000000",
                    usd_value=Decimal("12"),
                ),
            ]

    class Model:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "wallet_query"}

        async def plan_read(self, request):
            if request["read_loop_count"]:
                return None
            return {
                "name": "wallet_balance",
                "arguments": {"chain": "BSC", "address": "0x" + "1" * 40},
            }

    result = await build_graph(model=Model(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {
                "message": "我的 BNB 余额",
                "address": "0x" + "1" * 40,
                "chain": "BSC",
            },
            "wallet_context": {"address": "0x" + "1" * 40, "chain": "BSC"},
        },
        config={"configurable": {"thread_id": "asset-scoped-bnb"}},
    )

    assert result["response"]["kind"] == "wallet_query"
    assert result["response"]["asset_scope"]["symbol"] == "BNB"
    assert result["response"]["asset_count"] == 1
    assert "BNB" in result["response"]["message"]
    assert "USDC" not in result["response"]["message"]


@pytest.mark.asyncio
async def test_all_chain_balance_filters_assets_with_zero_usd_value():
    """All-chain balance responses include only assets whose USD value is positive."""
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance
    from wallet_agent.graph.build import build_graph

    class Wallet:
        chain_index_by_name = {"ETH": "1", "BASE": "8453"}
        chain_name_by_index = {"1": "ETH", "8453": "BASE"}

        async def get_token_balances(self, _address, chain_indexes):
            assert chain_indexes == ["1", "8453"]
            return [
                TokenBalance(
                    asset=Asset(chain="ETH", chain_id=1, symbol="ETH", decimals=18),
                    amount=Decimal("0.1"),
                    amount_raw="100000000000000000",
                    usd_value=Decimal("250"),
                ),
                TokenBalance(
                    asset=Asset(
                        chain="BASE",
                        chain_id=8453,
                        symbol="DUST",
                        decimals=18,
                        address="0x" + "3" * 40,
                    ),
                    amount=Decimal("1"),
                    amount_raw="1000000000000000000",
                    usd_value=Decimal("0"),
                ),
            ]

        async def get_total_value(self, _address, chain_indexes):
            return {"total_value": "250", "chain_indexes": chain_indexes}

    class Model:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "wallet_query"}

        async def invoke_read_tools(self, messages, _tools):
            if not any(isinstance(item, ToolMessage) for item in messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-all-positive",
                            "name": "portfolio_snapshot",
                            "args": {"address": "0x" + "1" * 40},
                        }
                    ],
                )
            return AIMessage(content="")

    result = await build_graph(model=Model(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {
                "message": "我的余额",
                "address": "0x" + "1" * 40,
                "chain": "ETH",
            },
            "wallet_context": {"address": "0x" + "1" * 40, "chain": "ETH"},
        },
        config={"configurable": {"thread_id": "all-positive-balances"}},
    )

    assert result["response"]["scope"]["requested"] == "all"
    assert result["response"]["asset_count"] == 1
    assert [item["asset"]["symbol"] for item in result["response"]["assets"]] == ["ETH"]
    assert "DUST" not in result["response"]["message"]


@pytest.mark.asyncio
async def test_asset_symbol_is_not_reused_from_previous_portfolio_result():
    """A new explicit symbol request must be scoped to that symbol, not stale facts."""
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance
    from wallet_agent.graph.build import build_graph

    class Wallet:
        chain_index_by_name = {"BSC": "56"}
        chain_name_by_index = {"56": "BSC"}

        async def get_token_balances(self, _address, chain_indexes):
            return [
                TokenBalance(
                    asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
                    amount=Decimal("0.5"),
                    amount_raw="500000000000000000",
                    usd_value=Decimal("150"),
                ),
            ]

    class Model:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "wallet_query"}

        async def plan_read(self, request):
            if request["read_loop_count"]:
                return None
            return {
                "name": "wallet_balance",
                "arguments": {"chain": "BSC", "address": "0x" + "1" * 40},
            }

    result = await build_graph(model=Model(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {
                "message": "我的 USDC 余额",
                "address": "0x" + "1" * 40,
                "chain": "BSC",
            },
            "wallet_context": {"address": "0x" + "1" * 40, "chain": "BSC"},
            "conversation_history": [
                {
                    "role": "assistant",
                    "artifacts": {
                        "response": {
                            "kind": "portfolio_query",
                            "assets": [{"asset": {"symbol": "BNB"}, "amount": "0.5"}],
                        }
                    },
                }
            ],
        },
        config={"configurable": {"thread_id": "fresh-asset-scope"}},
    )

    assert result["response"]["asset_scope"]["symbol"] == "USDC"
    assert result["response"]["asset_count"] == 0
    assert "BNB" not in result["response"]["message"]


@pytest.mark.asyncio
async def test_asset_scoped_balance_rejects_model_reply_with_other_symbols():
    """The response model cannot expand a BNB-only result back to a portfolio."""
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance
    from wallet_agent.graph.build import build_graph
    from wallet_agent.models.contracts import AgentResponseDraft

    class Wallet:
        chain_index_by_name = {"BSC": "56"}
        chain_name_by_index = {"56": "BSC"}

        async def get_token_balances(self, _address, _chain_indexes):
            return [
                TokenBalance(
                    asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
                    amount=Decimal("0.5"),
                    amount_raw="500000000000000000",
                    usd_value=Decimal("150"),
                ),
                TokenBalance(
                    asset=Asset(
                        chain="BSC",
                        chain_id=56,
                        symbol="USDC",
                        decimals=6,
                        address="0x" + "2" * 40,
                    ),
                    amount=Decimal("12"),
                    amount_raw="12000000",
                    usd_value=Decimal("12"),
                ),
            ]

    class Model:
        read_agent_enabled = True

        async def classify(self, _request):
            return {"intent": "wallet_query"}

        async def plan_read(self, _request):
            raise AssertionError("balance scope is deterministic")

        async def respond(self, _request, _facts):
            return AgentResponseDraft(
                language="zh",
                message="你的 BNB 余额是 0.5，另外还有 USDC。",
                suggestions=[],
            )

    result = await build_graph(model=Model(), wallet_provider=Wallet()).ainvoke(
        {
            "request": {
                "message": "我的 BNB 余额",
                "address": "0x" + "1" * 40,
                "chain": "BSC",
            },
            "wallet_context": {"address": "0x" + "1" * 40, "chain": "BSC"},
        },
        config={"configurable": {"thread_id": "asset-scope-response-guard"}},
    )

    assert result["response"]["asset_scope"] == {"symbol": "BNB"}
    assert "BNB" in result["response"]["message"]
    assert "USDC" not in result["response"]["message"]
