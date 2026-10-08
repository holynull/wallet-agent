from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from wallet_agent.domain.models import Asset, AssetQuery, NormalizedQuote

WALLET = "0x" + "1" * 40


class WalletProvider:
    chain_index_by_name = {"BSC": "56"}

    async def get_token_balances(self, address, indexes):
        assert address == WALLET
        assert indexes == ["56"]
        from wallet_agent.domain.models import Asset, TokenBalance

        return [
            TokenBalance(
                asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
                amount=Decimal("0.5"),
                amount_raw="500000000000000000",
                usd_value=Decimal("384"),
            )
        ]


class PriceProvider:
    async def get_prices(self, assets):
        from wallet_agent.domain.models import TokenPrice

        return [
            TokenPrice(asset=asset, usd_price=Decimal("768"), provider="okx")
            for asset in assets
        ]


class ReActModel:
    def __init__(self):
        self.calls = []

    async def invoke_react(self, messages, tools):
        assert all(
            "state" not in tool.tool_call_schema.model_json_schema().get("properties", {})
            for tool in tools
        )
        self.calls.append((list(messages), [tool.name for tool in tools]))
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "balance-1",
                        "name": "wallet_balance",
                        "args": {"chain": "BSC", "address": WALLET, "symbol": "BNB"},
                    }
                ],
            )
        if len(tool_messages) == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "price-1",
                        "name": "price_lookup",
                        "args": {"chain": "BSC", "symbol": "BNB"},
                    }
                ],
            )
        return AIMessage(content="你的 BNB 余额是 0.5，按 768 美元计算，持仓价值约 384 美元。")

    async def classify(self, _request):
        raise AssertionError("ordinary ReAct turns must not call the legacy classifier")

    async def extract(self, *_args):
        raise AssertionError("ordinary ReAct turns must not call the legacy extractor")


@pytest.mark.asyncio
async def test_unified_react_uses_balance_and_price_from_full_context():
    from wallet_agent.graph.build import build_graph

    model = ReActModel()
    graph = build_graph(
        model=model,
        wallet_provider=WalletProvider(),
        price_provider=PriceProvider(),
    )
    result = await graph.ainvoke(
        {
            "request": {
                "message": "BNB 价值多少 U",
                "address": WALLET,
                "chain": "BSC",
            },
            "wallet_context": {"address": WALLET, "chain": "BSC"},
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "unified-react-value"}},
    )

    assert result["response"]["kind"] == "react"
    assert "384" in result["response"]["message"]
    assert [type(item).__name__ for item in result["messages"]] == [
        "SystemMessage",
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert model.calls[0][1] == [
        "wallet_balance",
        "portfolio_snapshot",
        "price_lookup",
        "market_details",
        "price_history",
        "asset_discovery",
        "gas_estimate",
        "transaction_status",
        "get_swap_quote",
        "prepare_transfer",
        "query_swap_status",
    ]


@pytest.mark.asyncio
async def test_unified_react_exposes_balance_and_price_tool_observations_for_calculation():
    from wallet_agent.graph.build import build_graph

    model = ReActModel()
    graph = build_graph(
        model=model,
        wallet_provider=WalletProvider(),
        price_provider=PriceProvider(),
    )
    result = await graph.ainvoke(
        {
            "request": {
                "message": "BNB 价值多少 U",
                "address": WALLET,
                "chain": "BSC",
            },
            "wallet_context": {"address": WALLET, "chain": "BSC"},
        },
        config={"configurable": {"thread_id": "unified-react-value-facts"}},
    )

    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert len(tool_messages) == 2
    assert '"amount": "0.5"' in tool_messages[0].content
    assert '"usd_price": "768"' in tool_messages[1].content
    assert "384" in result["response"]["message"]


@pytest.mark.asyncio
async def test_unified_react_preserves_prior_history_for_short_followup():
    from wallet_agent.graph.build import build_graph

    class FollowupModel(ReActModel):
        async def invoke_react(self, messages, tools):
            self.calls.append((list(messages), [tool.name for tool in tools]))
            assert any(
                isinstance(item, HumanMessage)
                and item.content == "我想把 BNB 换成 ETH"
                for item in messages
            )
            assert messages[-1].content == "以太坊网络"
            return AIMessage(content="好的，我会按以太坊网络继续处理。")

    model = FollowupModel()
    graph = build_graph(model=model, wallet_provider=WalletProvider())
    result = await graph.ainvoke(
        {
            "request": {"message": "以太坊网络"},
            "conversation_history": [
                {"role": "user", "content": "我想把 BNB 换成 ETH"},
                {"role": "assistant", "content": "请确认目标网络。"},
            ],
        },
        config={"configurable": {"thread_id": "unified-react-followup"}},
    )

    assert result["response"]["message"] == "好的，我会按以太坊网络继续处理。"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_unified_react_does_not_mix_previous_turn_tools_into_new_response():
    from langgraph.checkpoint.memory import MemorySaver

    from wallet_agent.graph.build import build_graph

    class TwoTurnModel(ReActModel):
        def __init__(self):
            super().__init__()
            self.turn = 0

        async def invoke_react(self, messages, tools):
            self.turn += 1
            if self.turn == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "old-balance",
                            "name": "wallet_balance",
                            "args": {"chain": "BSC", "address": WALLET, "symbol": "BNB"},
                        },
                        {
                            "id": "old-price",
                            "name": "price_lookup",
                            "args": {"chain": "BSC", "symbol": "BNB"},
                        },
                    ],
                )
            return AIMessage(content="BNB 当前价格约为 768 美元。")

    model = TwoTurnModel()
    graph = build_graph(
        model=model,
        wallet_provider=WalletProvider(),
        price_provider=PriceProvider(),
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "unified-react-turn-boundary"}}
    await graph.ainvoke(
        {
            "request": {"message": "BNB 价值多少 U", "address": WALLET, "chain": "BSC"},
            "wallet_context": {"address": WALLET, "chain": "BSC"},
        },
        config=config,
    )
    second = await graph.ainvoke(
        {
            "request": {"message": "BNB 现在多少钱", "address": WALLET, "chain": "BSC"},
            "wallet_context": {"address": WALLET, "chain": "BSC"},
            "conversation_history": [
                {"role": "user", "content": "BNB 价值多少 U"},
                {"role": "assistant", "content": "你的 BNB 持仓价值约 384 美元。"},
                {"role": "user", "content": "BNB 现在多少钱"},
            ],
        },
        config=config,
    )

    assert second["response"]["kind"] == "react"


@pytest.mark.asyncio
async def test_unified_react_normalizes_api_seed_messages_before_first_model_call():
    from wallet_agent.graph.build import build_graph

    class SeedMessageModel(ReActModel):
        async def invoke_react(self, messages, tools):
            assert isinstance(messages[0], SystemMessage)
            assert any(
                isinstance(item, HumanMessage) and item.content == "ETH 的价格"
                for item in messages
            )
            return AIMessage(content="ETH 当前价格查询完成。")

    model = SeedMessageModel()
    graph = build_graph(model=model)
    result = await graph.ainvoke(
        {
            "request": {"message": "ETH 的价格"},
            "messages": [{"role": "user", "content": "ETH 的价格"}],
            "conversation_history": [{"role": "user", "content": "ETH 的价格"}],
        },
        config={"configurable": {"thread_id": "unified-react-api-seed"}},
    )

    assert result["response"]["message"] == "ETH 当前价格查询完成。"


def test_react_state_uses_add_messages_reducer():
    from wallet_agent.graph.state import AgentState

    assert "messages" in AgentState.__annotations__


@pytest.mark.asyncio
async def test_unified_react_keeps_swap_quotes_available_to_explicit_lifecycle_ui():
    from wallet_agent.graph.build import build_graph

    source = Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x1")
    destination = Asset(chain="BASE", chain_id=8453, symbol="USDT", decimals=6, address="0x2")

    class SwapProvider:
        provider_name = "bridgers"

        async def list_assets(self, query):
            assert isinstance(query, AssetQuery)
            if query.search == "USDC":
                return [source]
            if query.search == "USDT":
                return [destination]
            return []

        async def quote(self, request):
            return NormalizedQuote(
                provider=self.provider_name,
                source_asset=request.source_asset,
                destination_asset=request.destination_asset,
                input_amount=request.input_amount,
                input_amount_raw=request.input_amount_raw,
                expected_output=Decimal("0.99"),
                expected_output_raw="990000",
                provider_reference="quote-1",
            )

        async def prepare(self, _quote):
            raise AssertionError("ReAct must leave swap confirmation to the lifecycle API")

    class QuoteModel(ReActModel):
        async def invoke_react(self, messages, tools):
            self.calls.append((list(messages), [tool.name for tool in tools]))
            observations = [item for item in messages if isinstance(item, ToolMessage)]
            if not observations:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "quote-1-call",
                            "name": "get_swap_quote",
                            "args": {
                                "source_symbol": "USDC",
                                "destination_symbol": "USDT",
                                "input_amount": "1",
                                "source_chain": "BASE",
                                "destination_chain": "BASE",
                            },
                        }
                    ],
                )
            return AIMessage(content="我找到了报价，请在页面选择后继续确认。")

    result = await build_graph(model=QuoteModel(), providers=[SwapProvider()]).ainvoke(
        {
            "request": {"message": "在 Base 把 1 USDC 换成 USDT"},
            "wallet_context": {"address": "0x" + "1" * 40, "chain": "BASE"},
        },
        config={"configurable": {"thread_id": "unified-react-quote-lifecycle"}},
    )

    assert result["response"]["kind"] == "react"
    assert result.get("quote_candidates"), [
        item.content for item in result["messages"] if isinstance(item, ToolMessage)
    ]
    assert result["quote_candidates"][0]["provider_reference"] == "quote-1"
    assert result["swap_request"]["source_asset"]["symbol"] == "USDC"
    assert result["selected_quote"] is None


@pytest.mark.asyncio
async def test_unified_react_tool_loop_stops_after_bounded_unique_calls():
    from wallet_agent.graph.build import build_graph

    class LongLoopModel(ReActModel):
        async def invoke_react(self, messages, tools):
            self.calls.append((list(messages), [tool.name for tool in tools]))
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"asset-{len(self.calls)}",
                        "name": "asset_discovery",
                        "args": {"chain": "BASE", "search": f"asset-{len(self.calls)}"},
                    }
                ],
            )

    model = LongLoopModel()
    result = await build_graph(model=model).ainvoke(
        {"request": {"message": "查一下这些资产"}},
        config={"configurable": {"thread_id": "unified-react-step-limit"}},
    )

    assert len(model.calls) == 8
    assert result["react_steps"] == 9
    assert result["response"]["kind"] == "react"
    assert "有限次数" in result["response"]["message"]


@pytest.mark.asyncio
async def test_unified_react_tool_timeout_returns_safe_tool_message_and_timing():
    import asyncio

    from wallet_agent.graph.build import build_graph

    class SlowPrices:
        async def get_prices(self, _assets):
            await asyncio.sleep(0.1)
            return []

    class TimeoutModel(ReActModel):
        async def invoke_react(self, messages, tools):
            self.calls.append((list(messages), [tool.name for tool in tools]))
            last_user = max(
                (index for index, item in enumerate(messages) if isinstance(item, HumanMessage)),
                default=-1,
            )
            if not any(isinstance(item, ToolMessage) for item in messages[last_user + 1 :]):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "slow-price-call",
                            "name": "price_lookup",
                            "args": {"chain": "ETH", "symbol": "ETH"},
                        }
                    ],
                )
            return AIMessage(content="资产目录查询超时，我暂时无法确认该资产。")

    result = await build_graph(
        model=TimeoutModel(),
        price_provider=SlowPrices(),
        react_tool_timeout_seconds=0.01,
    ).ainvoke(
        {"request": {"message": "查询 ETH 的价格"}},
        config={"configurable": {"thread_id": "react-tool-timeout"}},
    )

    observations = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert len(observations) == 1
    assert observations[0].tool_call_id == "slow-price-call"
    assert '"code": "REACT_TOOL_TIMEOUT"' in observations[0].content
    assert result["react_tool_timings"][0]["status"] == "timeout"
    assert result["react_tool_timings"][0]["tool"] == "price_lookup"
    assert result["response"]["react_tool_timings"] == result["react_tool_timings"]


@pytest.mark.asyncio
async def test_unified_react_caches_only_asset_metadata_and_records_hit_miss():
    from wallet_agent.domain.models import Asset
    from wallet_agent.graph.build import build_graph

    class AssetProvider:
        provider_name = "catalog"

        def __init__(self):
            self.calls = 0

        async def list_assets(self, _query):
            self.calls += 1
            return [Asset(chain="BASE", symbol="USDC", decimals=6, address="0x1")]

    class AssetModel(ReActModel):
        async def invoke_react(self, messages, tools):
            self.calls.append((list(messages), [tool.name for tool in tools]))
            last_user = max(
                (index for index, item in enumerate(messages) if isinstance(item, HumanMessage)),
                default=-1,
            )
            if not any(isinstance(item, ToolMessage) for item in messages[last_user + 1 :]):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"asset-{len(self.calls)}",
                            "name": "asset_discovery",
                            "args": {"chain": "BASE", "search": "USDC"},
                        }
                    ],
                )
            return AIMessage(content="找到 USDC。")

    provider = AssetProvider()
    graph = build_graph(model=AssetModel(), providers=[provider])
    config = {"configurable": {"thread_id": "react-asset-cache"}}

    first = await graph.ainvoke({"request": {"message": "查找 USDC"}}, config=config)
    second = await graph.ainvoke({"request": {"message": "再查找 USDC"}}, config=config)

    assert provider.calls == 1
    assert first["react_tool_timings"][0]["cache_status"] == "miss"
    assert second["react_tool_timings"][0]["cache_status"] == "hit"
    assert graph.metrics.snapshot()["tools"]["asset_discovery"]["cache_hits"] == 1


@pytest.mark.asyncio
async def test_unified_react_sanitizes_provider_exception_details_before_toolmessage():
    from wallet_agent.graph.build import build_graph

    class FailingAssetProvider:
        provider_name = "catalog"

        async def list_assets(self, _query):
            raise RuntimeError("https://catalog.invalid?api_key=never-expose")

    class ErrorModel(ReActModel):
        async def invoke_react(self, messages, tools):
            self.calls.append((list(messages), [tool.name for tool in tools]))
            last_user = max(
                (index for index, item in enumerate(messages) if isinstance(item, HumanMessage)),
                default=-1,
            )
            if not any(isinstance(item, ToolMessage) for item in messages[last_user + 1 :]):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "asset-error",
                            "name": "asset_discovery",
                            "args": {"chain": "BASE", "search": "USDC"},
                        }
                    ],
                )
            return AIMessage(content="我暂时无法查询这个 Token。")

    result = await build_graph(
        model=ErrorModel(), providers=[FailingAssetProvider()]
    ).ainvoke(
        {"request": {"message": "查找 USDC"}},
        config={"configurable": {"thread_id": "react-safe-error"}},
    )

    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages
    assert "never-expose" not in tool_messages[0].content
    assert "never-expose" not in str(result["response"]["react_results"])
