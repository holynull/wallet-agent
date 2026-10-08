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
