import pytest

from wallet_agent.graph.read_only import (
    READ_ONLY_TOOL_NAMES,
    ReadOnlyToolCall,
    route_read_only,
    run_read_only_round,
)


def test_registry_contains_only_read_capabilities():
    assert READ_ONLY_TOOL_NAMES == frozenset(
        {
            "wallet_balance",
            "price_lookup",
            "gas_estimate",
            "asset_discovery",
            "transaction_status",
        }
    )


def test_route_stops_after_two_rounds():
    assert route_read_only({"read_loop_count": 0, "read_tool_call": None}) == "agent"
    assert route_read_only(
        {"read_loop_count": 0, "read_tool_call": {"name": "price_lookup"}}
    ) == "tool"
    assert route_read_only(
        {"read_loop_count": 2, "read_tool_call": {"name": "price_lookup"}}
    ) == "response"
    assert route_read_only(
        {"read_loop_count": 1, "read_tool_call": None, "read_only_complete": True}
    ) == "response"


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
    assert model.rounds == 2
    assert result["read_loop_count"] == 2
    assert result["read_only_enabled"] is True
    assert result["read_only_complete"] is True
    assert result.get("transfer_request") is None
    assert result.get("swap_request") is None


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
