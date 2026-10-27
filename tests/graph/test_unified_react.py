import json
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from wallet_agent.domain.models import (
    Asset,
    AssetQuery,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
)

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
        "receive_address",
        "get_swap_quote",
        "swap_quick_replies",
        "transaction_history",
        "transfer_quick_replies",
        "request_client_action",
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
async def test_unified_react_defaults_swap_quote_for_lifecycle_confirmation():
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
    # The design defaults to one channel once the swap is complete: the react
    # turn auto-selects the best quote so the API continues into confirmation
    # without the channel picker.
    assert result["selected_quote"]["provider_reference"] == "quote-1"


@pytest.mark.asyncio
async def test_react_swap_rejects_invalid_amount_mode_instead_of_treating_output_as_input():
    from wallet_agent.graph.build import build_graph

    class Model:
        async def invoke_react(self, messages, tools):
            if not any(isinstance(item, ToolMessage) for item in messages):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "invalid-mode",
                            "name": "get_swap_quote",
                            "args": {
                                "source_symbol": "BNB",
                                "destination_symbol": "ETH",
                                "source_chain": "BSC",
                                "destination_chain": "ETH",
                                "output_amount": "0.004",
                                "amount_mode": "output",
                            },
                        }
                    ],
                )
            return AIMessage(content="参数模式无效，请重新确认目标数量。")

    result = await build_graph(model=Model()).ainvoke(
        {"request": {"message": "bnb 兑换 10u 的 ETH"}},
        config={"configurable": {"thread_id": "react-invalid-amount-mode"}},
    )

    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages
    assert '"code": "INVALID_AMOUNT_MODE"' in tool_messages[0].content
    assert not result.get("quote_candidates")


@pytest.mark.asyncio
async def test_react_step_limit_keeps_latest_valid_quote_and_natural_language_answer():
    from wallet_agent.graph.build import build_graph

    source = Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18)
    destination = Asset(chain="ETH", chain_id=1, symbol="ETH", decimals=18)
    valid_quote = {
        "provider": "bridgers",
        "source_asset": source.model_dump(mode="json"),
        "destination_asset": destination.model_dump(mode="json"),
        "input_amount": "0.013548",
        "input_amount_raw": "13548000000000000",
        "expected_output": "0.003851",
        "expected_output_raw": "3851000000000000",
        "provider_reference": "valid-quote",
    }

    class Model:
        async def invoke_react(self, messages, tools):
            tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
            if not tool_messages:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "valid-call",
                            "name": "get_swap_quote",
                            "args": {
                                "source_symbol": "BNB",
                                "destination_symbol": "ETH",
                                "source_chain": "BSC",
                                "destination_chain": "ETH",
                                "input_amount": "0.013548",
                            },
                        }
                    ],
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"repeat-{len(tool_messages)}",
                        "name": "asset_discovery",
                        "args": {"chain": "BSC", "search": f"OTHER-{len(tool_messages)}"},
                    }
                ],
            )

    class Provider:
        provider_name = "bridgers"

        async def list_assets(self, query):
            if query.search == "BNB":
                return [source]
            return [destination] if query.chain == "ETH" else []

        async def quote(self, request):
            return NormalizedQuote.model_validate(valid_quote)

    result = await build_graph(model=Model(), providers=[Provider()], max_react_steps=2).ainvoke(
        {
            "request": {"message": "bnb 兑换 10u 的 ETH"},
            "wallet_context": {"address": WALLET, "chain": "BSC"},
        },
        config={"configurable": {"thread_id": "react-limit-preserves-quote"}},
    )

    assert result["response"]["route_reason"] == "react_step_limit_with_result"
    assert "有限次数" not in result["response"]["message"]
    assert result["quote_candidates"][0]["provider_reference"] == "valid-quote"


def test_react_quote_projection_ignores_zero_output_quote():
    from wallet_agent.graph.nodes import _best_react_quote_result

    result = _best_react_quote_result(
        [
            (
                "get_swap_quote",
                {
                    "ok": True,
                    "quotes": [
                        {
                            "provider": "bridgers",
                            "provider_reference": "zero",
                            "input_amount": "1",
                            "expected_output": "0",
                        }
                    ],
                },
            ),
            (
                "get_swap_quote",
                {
                    "ok": True,
                    "quotes": [
                        {
                            "provider": "omnibridge",
                            "provider_reference": "valid",
                            "input_amount": "0.013548",
                            "expected_output": "0.0039",
                        }
                    ],
                },
            ),
        ]
    )

    assert result is not None
    assert [item["provider_reference"] for item in result["quotes"]] == ["valid"]


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


def test_plain_text_message_strips_markdown_images_and_tables():
    from wallet_agent.graph.nodes import _plain_text_message

    assert (
        _plain_text_message(
            "你的 ETH 余额是 ![ETH](https://cdn.example/eth/logo.png) 1.2。"
        )
        == "你的 ETH 余额是 ETH 1.2。"
    )
    assert (
        _plain_text_message(
            "支持的网络：\n"
            "| 链 | 网络 |\n"
            "|---|---|\n"
            "| EVM | Ethereum、BSC |\n"
            "| TRON | 波场 |"
        )
        == "支持的网络：\n链，网络\nEVM，Ethereum、BSC\nTRON，波场"
    )
    # Plain text, lists, and code blocks are untouched.
    assert _plain_text_message("普通文本，没有 markdown。") == "普通文本，没有 markdown。"
    assert (
        _plain_text_message("地址：\n```\n0x123\n```\n- A\n- B")
        == "地址：\n```\n0x123\n```\n- A\n- B"
    )


def test_plain_text_message_strips_embedded_suggestions_blocks():
    from wallet_agent.graph.nodes import _plain_text_message

    assert (
        _plain_text_message(
            "收款地址已收到：TPtbB3MT4o7rQ7vpEMW8BWquwfJvoAkHia\n"
            "\n"
            "请选择转账网络。\n"
            "\n"
            "<suggestions>\n"
            '[{"label": "Use TRON", "type": "chain", "value": "TRON",'
            ' "chain_logo_url": "https://…/tron/info/logo.png",'
            ' "data": {"intent": "transfer", "chain": "TRON"}}]\n'
            "</suggestions>"
        )
        == "收款地址已收到：TPtbB3MT4o7rQ7vpEMW8BWquwfJvoAkHia\n\n请选择转账网络。"
    )
    assert _plain_text_message("先选网络\n<options>a、b</options>\n继续") == "先选网络\n\n继续"


@pytest.mark.asyncio
async def test_unified_react_message_is_plain_text_even_when_model_embeds_markdown():
    from wallet_agent.graph.build import build_graph

    LOGO = "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/ethereum/info/logo.png"

    class MarkdownModel(ReActModel):
        async def invoke_react(self, messages, tools):
            self.calls.append((list(messages), [tool.name for tool in tools]))
            return AIMessage(
                content=(
                    "你的 ETH 余额是 0.5，按 768 美元计算，持仓价值约 384 美元。\n"
                    f"![ETH]({LOGO}) 详情：\n"
                    "| 资产 | 价值 |\n"
                    "|---|---|\n"
                    "| BNB | 384 美元 |"
                )
            )

    result = await build_graph(model=MarkdownModel()).ainvoke(
        {"request": {"message": "我的 BNB 价值多少"}},
        config={"configurable": {"thread_id": "react-plain-text"}},
    )

    message = result["response"]["message"]
    assert LOGO not in message
    assert "![" not in message
    assert "|---|---|" not in message
    assert "384 美元" in message


@pytest.mark.asyncio
async def test_unified_react_message_strips_embedded_suggestions_json():
    from wallet_agent.graph.build import build_graph

    class SuggestionsModel(ReActModel):
        async def invoke_react(self, messages, tools):
            self.calls.append((list(messages), [tool.name for tool in tools]))
            return AIMessage(
                content=(
                    "收款地址已收到：TPtbB3MT4o7rQ7vpEMW8BWquwfJvoAkHia\n"
                    "\n"
                    "请选择转账网络。\n"
                    "\n"
                    "<suggestions>\n"
                    '[{"label": "Use TRON", "type": "chain", "value": "TRON"}]\n'
                    "</suggestions>"
                )
            )

    result = await build_graph(model=SuggestionsModel()).ainvoke(
        {"request": {"message": "转给 TPtbB3MT4o7rQ7vpEMW8BWquwfJvoAkHia"}},
        config={"configurable": {"thread_id": "react-no-suggestions-in-message"}},
    )

    message = result["response"]["message"]
    assert "<suggestions>" not in message
    assert "</suggestions>" not in message
    assert '"type"' not in message
    assert "请选择转账网络" in message


class EmptyQuoteProvider:
    """A provider whose quote resolves to an unusable (zero-output) quote, so
    get_swap_quote returns NO_VALID_QUOTES with a deterministic message."""

    provider_name = "empty"

    async def list_assets(self, query):
        if query.search == "USDC":
            return [
                Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x1")
            ]
        if query.search == "USDT":
            return [
                Asset(chain="BASE", chain_id=8453, symbol="USDT", decimals=6, address="0x2")
            ]
        return []

    async def quote(self, request):
        return NormalizedQuote(
            provider="empty",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal("0"),
            expected_output_raw="0",
            provider_reference="empty",
        )


class NoQuoteModel:
    async def invoke_react(self, messages, tools):
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "nq-1",
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
        payload = json.loads(tool_messages[0].content)
        assert payload["ok"] is False
        assert payload["code"] == "NO_VALID_QUOTES"
        assert "暂时没有可用的兑换报价" in payload["message"]
        return AIMessage(content="暂时没有可用的兑换报价。")


@pytest.mark.asyncio
async def test_react_swap_no_valid_quotes_carries_a_deterministic_message():
    from wallet_agent.graph.build import build_graph

    result = await build_graph(
        model=NoQuoteModel(), providers=[EmptyQuoteProvider()]
    ).ainvoke(
        {
            "request": {"message": "在 Base 把 1 USDC 换成 USDT"},
            "wallet_context": {"address": WALLET, "chain": "BASE"},
        },
        config={"configurable": {"thread_id": "react-no-valid-quotes"}},
    )

    assert "暂时没有可用的兑换报价" in result["response"]["message"]


class SwapArrivalProvider:
    provider_name = "bridgers"

    async def get_status(self, order):
        assert order.provider_order_id == "active-swap-order"
        return NormalizedOrderStatus(
            provider="bridgers",
            provider_order_id="active-swap-order",
            provider_reference="active-ref",
            status="completed",
            tx_hash="0x" + "c" * 64,
            eta="1-2 分钟",
        )


class SwapArrivalModel:
    """A chat follow-up after a signed swap must be steered to
    query_swap_status, and the tool must resolve the active order from state
    even when the model passes no order_id argument."""

    def __init__(self):
        self.system_snapshot = ""

    async def invoke_react(self, messages, tools):
        tool_names = [tool.name for tool in tools]
        assert "query_swap_status" in tool_names
        assert "transaction_history" in tool_names
        system = next(
            (item for item in messages if isinstance(item, SystemMessage)), None
        )
        self.system_snapshot = str(system.content) if system else ""
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "swap-status-1",
                        "name": "query_swap_status",
                        "args": {},
                    }
                ],
            )
        payload = json.loads(tool_messages[0].content)
        assert payload["ok"] is True
        assert payload["kind"] == "swap_status"
        assert payload["provider_order_id"] == "active-swap-order"
        return AIMessage(content="这笔兑换已完成到账，可以到目标链查看到账资产。")


@pytest.mark.asyncio
async def test_react_arrival_followup_returns_swap_order_not_transaction_history():
    from wallet_agent.graph.build import build_graph

    model = SwapArrivalModel()
    graph = build_graph(
        model=model,
        providers=[SwapArrivalProvider()],
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "到账了没有？"},
            "wallet_context": {"address": WALLET, "chain": "ETH"},
            "conversation_history": [
                {"role": "user", "content": "USDC 全部换成 ETH"},
                {"role": "assistant", "content": "兑换已确认广播，正在等待到账。"},
            ],
            "provider_order_ids": {"bridgers": "active-swap-order"},
            "provider_orders": {
                "bridgers": ProviderOrder(
                    provider="bridgers",
                    provider_order_id="active-swap-order",
                    provider_reference="active-ref",
                    tx_hash="0x" + "c" * 64,
                ).model_dump(mode="json")
            },
            "broadcast_tx_hash": "0x" + "c" * 64,
            "broadcast_status": "source_confirmed",
        },
        config={"configurable": {"thread_id": "react-arrival-followup"}},
    )

    # The chat follow-up stays on the ordinary ReAct path...
    assert result["response"]["kind"] == "react"
    assert "到账" in result["response"]["message"]
    # ...but the model was told about the active order and answered from the
    # swap order status, not from a recent-transaction query.
    assert "进行中的兑换订单" in model.system_snapshot
    assert "active-swap-order" in model.system_snapshot
    query_status = next(
        (
            item["result"]
            for item in result["response"]["react_results"]
            if item["tool"] == "query_swap_status"
        ),
        None,
    )
    assert query_status is not None
    assert query_status["kind"] == "swap_status"
    assert query_status["status"] == "completed"
    assert query_status["provider_order_id"] == "active-swap-order"
    assert not any(
        item["tool"] == "transaction_history"
        for item in result["response"]["react_results"]
    )


class SwapNotPropagatedArrivalModel:
    """A chat "到账了没有？" on a swap whose order has not reached the provider
    yet (broadcast_status == "not_propagated", no provider order id). The model
    only passes the source tx_hash it saw in the prompt; the tool must return a
    swap_status observation reflecting the pending provider pickup instead of a
    bare chain lookup, and the answer must not ask the user for an order id."""

    def __init__(self):
        self.system_snapshot = ""

    async def invoke_react(self, messages, tools):
        tool_names = [tool.name for tool in tools]
        assert "query_swap_status" in tool_names
        system = next(
            (item for item in messages if isinstance(item, SystemMessage)), None
        )
        self.system_snapshot = str(system.content) if system else ""
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "swap-status-2",
                        "name": "query_swap_status",
                        "args": {"tx_hash": "0x" + "e" * 64, "chain": "ETH"},
                    }
                ],
            )
        payload = json.loads(tool_messages[0].content)
        assert payload["ok"] is True
        assert payload["kind"] == "swap_status"
        assert payload["provider_order_id"] is None
        assert payload["status"] == "not_propagated"
        return AIMessage(content="还没到账，兑换订单还在等待服务商接收，请稍后重新查询。")


@pytest.mark.asyncio
async def test_react_arrival_followup_not_propagated_reports_pending_provider_pickup():
    from wallet_agent.graph.build import build_graph

    model = SwapNotPropagatedArrivalModel()
    graph = build_graph(model=model)
    result = await graph.ainvoke(
        {
            "request": {"message": "到账了没有？"},
            "wallet_context": {"address": WALLET, "chain": "ETH"},
            "conversation_history": [
                {"role": "user", "content": "用 ETH 兑换 10 USDC"},
                {"role": "assistant", "content": "报价已展示，请在 App 中确认。"},
            ],
            "broadcast_tx_hash": "0x" + "e" * 64,
            "broadcast_status": "not_propagated",
            "selected_quote": {
                "provider": "omnibridge",
                "provider_reference": "omni-ref",
            },
        },
        config={"configurable": {"thread_id": "react-arrival-not-propagated"}},
    )

    assert result["response"]["kind"] == "react"
    # The prompt surfaces the broadcast stage and forbids asking for an order id.
    assert "进行中的兑换订单" in model.system_snapshot
    assert "广播状态 not_propagated" in model.system_snapshot
    assert "不要向用户索要订单号" in model.system_snapshot
    query_status = next(
        (
            item["result"]
            for item in result["response"]["react_results"]
            if item["tool"] == "query_swap_status"
        ),
        None,
    )
    assert query_status is not None
    assert query_status["kind"] == "swap_status"
    assert query_status["status"] == "not_propagated"
    assert query_status["provider_order_id"] is None
    assert "等待 Provider 接收" in query_status["message"]
    assert not any(
        item["tool"] == "transaction_history"
        for item in result["response"]["react_results"]
    )


class RecoverArrivalAdapter:
    async def get_transaction_receipt(self, _tx_hash):
        return {"status": "0x1", "blockNumber": "0x10"}


class RecoverArrivalProvider:
    provider_name = "bridgers"

    def __init__(self):
        self.broadcast_calls = 0
        self.status_calls = 0

    async def register_broadcast(self, provider_reference, tx_hash, **kwargs):
        self.broadcast_calls += 1
        return ProviderOrder(
            provider="bridgers",
            provider_order_id="recovered-order",
            provider_reference=provider_reference,
            tx_hash=tx_hash,
        )

    async def get_status(self, order):
        self.status_calls += 1
        return NormalizedOrderStatus(
            provider="bridgers",
            provider_order_id=order.provider_order_id,
            provider_reference=order.provider_reference,
            status="completed",
            tx_hash=order.tx_hash,
            eta="1-2 分钟",
        )


class RecoverArrivalModel:
    async def invoke_react(self, messages, tools):
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "swap-status-3",
                        "name": "query_swap_status",
                        "args": {"tx_hash": "0x" + "d" * 64, "chain": "ETH"},
                    }
                ],
            )
        payload = json.loads(tool_messages[0].content)
        assert payload["ok"] is True
        assert payload["kind"] == "swap_status"
        assert payload["provider_order_id"] == "recovered-order"
        assert payload["status"] == "completed"
        return AIMessage(content="这笔兑换已经到账了。")


class RecoverPinnedProvider:
    provider_name = "omnibridge"

    async def register_broadcast(self, provider_reference, tx_hash, **kwargs):
        return ProviderOrder(
            provider="omnibridge",
            provider_order_id=f"order-{provider_reference}",
            provider_reference=provider_reference,
            tx_hash=tx_hash,
        )

    async def get_status(self, order):
        return NormalizedOrderStatus(
            provider="omnibridge",
            provider_order_id=order.provider_order_id,
            provider_reference=order.provider_reference,
            status="completed",
            tx_hash=order.tx_hash,
        )


class RecoverPinnedModel:
    async def invoke_react(self, messages, tools):
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "swap-status-4",
                        "name": "query_swap_status",
                        "args": {"tx_hash": "0x" + "a" * 64, "chain": "ETH"},
                    }
                ],
            )
        payload = json.loads(tool_messages[0].content)
        assert payload["ok"] is True
        assert payload["provider_order_id"] == "order-omni-ref"
        assert payload["status"] == "completed"
        return AIMessage(content="这笔兑换已经到账了。")


@pytest.mark.asyncio
async def test_react_arrival_recover_stale_not_propagated_broadcast():
    """A chat arrival check on a broadcast saved as not_propagated must recover
    the provider order once the source transaction confirms on-chain, instead of
    permanently reporting 'not_propagated' (Lif's report: swap had arrived)."""
    from wallet_agent.graph.build import build_graph

    model = RecoverArrivalModel()
    provider = RecoverArrivalProvider()
    graph = build_graph(
        model=model,
        providers=[provider],
        chains={"ETH": RecoverArrivalAdapter()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "到账了没有？"},
            "wallet_context": {"address": WALLET, "chain": "ETH"},
            "conversation_history": [
                {"role": "user", "content": "用 ETH 兑换 10 USDT"},
                {"role": "assistant", "content": "报价已展示，请在 App 中确认。"},
            ],
            "broadcast_tx_hash": "0x" + "d" * 64,
            "broadcast_status": "not_propagated",
            "swap_request": {
                "source_asset": {"chain": "ETH", "symbol": "ETH", "decimals": 18},
                "destination_asset": {"chain": "ETH", "symbol": "USDT", "decimals": 6},
                "input_amount": "0.0044",
            },
            "quote_candidates": [
                {
                    "provider": "bridgers",
                    "provider_reference": "recover-ref",
                    "source_asset": {
                        "chain": "ETH",
                        "symbol": "ETH",
                        "decimals": 18,
                    },
                    "destination_asset": {
                        "chain": "ETH",
                        "symbol": "USDT",
                        "decimals": 6,
                    },
                    "input_amount": "0.0044",
                    "expected_output": "10",
                }
            ],
        },
        config={"configurable": {"thread_id": "react-arrival-recover"}},
    )

    query_status = next(
        item["result"]
        for item in result["response"]["react_results"]
        if item["tool"] == "query_swap_status"
    )
    assert query_status["status"] == "completed"
    assert query_status["provider_order_id"] == "recovered-order"
    assert provider.broadcast_calls == 1
    assert provider.status_calls == 1


@pytest.mark.asyncio
async def test_react_arrival_recovery_keeps_the_pinned_provider():
    """Stale-broadcast recovery must reuse the provider the user actually chose
    (selected_provider_reference), not re-default to the highest-output quote."""
    from wallet_agent.graph.build import build_graph

    model = RecoverPinnedModel()
    provider = RecoverPinnedProvider()
    graph = build_graph(
        model=model,
        providers=[provider],
        chains={"ETH": RecoverArrivalAdapter()},
    )
    result = await graph.ainvoke(
        {
            "request": {"message": "到账了没有？"},
            "wallet_context": {"address": WALLET, "chain": "ETH"},
            "conversation_history": [
                {"role": "user", "content": "用 ETH 兑换 10 USDT"},
                {"role": "assistant", "content": "报价已展示，请在 App 中确认。"},
            ],
            "broadcast_tx_hash": "0x" + "a" * 64,
            "broadcast_status": "not_propagated",
            "selected_provider_reference": "omni-ref",
            "swap_request": {
                "source_asset": {"chain": "ETH", "symbol": "ETH", "decimals": 18},
                "destination_asset": {"chain": "ETH", "symbol": "USDT", "decimals": 6},
                "input_amount": "0.0044",
            },
            "quote_candidates": [
                {
                    "provider": "bridgers",
                    "provider_reference": "bridgers-ref",
                    "source_asset": {
                        "chain": "ETH",
                        "symbol": "ETH",
                        "decimals": 18,
                    },
                    "destination_asset": {
                        "chain": "ETH",
                        "symbol": "USDT",
                        "decimals": 6,
                    },
                    "input_amount": "0.0044",
                    "expected_output": "10.02",
                },
                {
                    "provider": "omnibridge",
                    "provider_reference": "omni-ref",
                    "source_asset": {
                        "chain": "ETH",
                        "symbol": "ETH",
                        "decimals": 18,
                    },
                    "destination_asset": {
                        "chain": "ETH",
                        "symbol": "USDT",
                        "decimals": 6,
                    },
                    "input_amount": "0.0044",
                    "expected_output": "10.00",
                },
            ],
        },
        config={"configurable": {"thread_id": "react-arrival-pinned-provider"}},
    )

    query_status = next(
        item["result"]
        for item in result["response"]["react_results"]
        if item["tool"] == "query_swap_status"
    )
    assert query_status["provider_order_id"] == "order-omni-ref"
    assert query_status["status"] == "completed"


def test_looks_like_balance_only_query_separates_read_from_transfer_steps():
    from wallet_agent.graph.nodes import _looks_like_balance_only_query

    assert _looks_like_balance_only_query("查一下余额") is True
    assert _looks_like_balance_only_query("我的资产总览") is True
    assert _looks_like_balance_only_query("看看 BNB 持仓价值") is True
    assert _looks_like_balance_only_query("用余额的 25% 转这笔") is False
    assert _looks_like_balance_only_query("继续转账 0.5 ETH") is False
    assert _looks_like_balance_only_query("转 0.5 ETH 给小明") is False


def _render_decision(**overrides):
    decision = {
        "intent": "swap",
        "source_symbol": None,
        "destination_symbol": None,
        "recommend_swap_pairs": False,
        "reason": "test",
    }
    decision.update(overrides)
    return decision


class ProseDecisionModel:
    """Answers in prose and owns the render decision via ``decide_render``."""

    def __init__(self, decision):
        self.decision = dict(decision)
        self.last_decision_request = None

    async def invoke_react(self, messages, tools):
        return AIMessage(content="好的。")

    async def decide_render(self, request):
        self.last_decision_request = request
        return dict(self.decision)


@pytest.mark.asyncio
async def test_arrival_status_followup_does_not_render_swap_recommendation():
    """A swap-arrival follow-up must never pop the "好的，想换什么？" card.

    The card decision is the model's: for a status follow-up the model opts out
    of ``recommend_swap_pairs``, so the graph renders prose only (the old
    keyword rule ``_swap_recommendation_desired`` missed phrasings like
    "到账了没有" and popped the wrong card).
    """
    from wallet_agent.graph.build import build_graph

    model = ProseDecisionModel(_render_decision(intent="status"))
    graph = build_graph(model=model)
    result = await graph.ainvoke(
        {"request": {"message": "到账了没有？"}, "conversation_history": []},
        config={"configurable": {"thread_id": "react-status-no-recommend"}},
    )

    assert model.last_decision_request is not None
    assert model.last_decision_request["message"] == "到账了没有？"
    suggestions = result["response"].get("suggestions") or []
    assert not any(str(item.get("type") or "") == "swap_pair" for item in suggestions)


@pytest.mark.asyncio
async def test_open_swap_intent_renders_swap_recommendation_via_model_decision():
    """An open-ended "帮我兑换" opts the model into the recommendation card."""
    from wallet_agent.graph.build import build_graph

    model = ProseDecisionModel(_render_decision(recommend_swap_pairs=True))
    graph = build_graph(model=model)
    result = await graph.ainvoke(
        {"request": {"message": "帮我兑换"}, "conversation_history": []},
        config={"configurable": {"thread_id": "react-open-swap-recommend"}},
    )

    suggestions = result["response"].get("suggestions") or []
    assert any(str(item.get("type") or "") == "swap_pair" for item in suggestions)


class SymbolScopedBalanceModel:
    """A swap-arrival check that looks up one symbol (USDC) and finds 0 must not
    be projected as the top-level "资产总览 $0.00" portfolio card."""

    def __init__(self):
        self.payload = None

    async def invoke_react(self, messages, tools):
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "scoped-balance-1",
                        "name": "wallet_balance",
                        "args": {"chain": "BSC", "symbol": "USDC"},
                    }
                ],
            )
        self.payload = json.loads(tool_messages[0].content)
        return AIMessage(content="你 BSC 上的 USDC 余额为 0，兑换目标资产尚未到账。")


@pytest.mark.asyncio
async def test_react_symbol_scoped_empty_balance_does_not_project_portfolio_card():
    from wallet_agent.graph.build import build_graph

    model = SymbolScopedBalanceModel()
    graph = build_graph(model=model, wallet_provider=WalletProvider())
    result = await graph.ainvoke(
        {
            "request": {"message": "到账了没有？", "address": WALLET, "chain": "BSC"},
            "wallet_context": {"address": WALLET, "chain": "BSC"},
            "broadcast_tx_hash": "0x" + "f" * 64,
            "broadcast_status": "not_propagated",
        },
        config={"configurable": {"thread_id": "react-scoped-balance-card"}},
    )

    assert result["response"]["kind"] == "react"
    assert model.payload is not None
    portfolio = model.payload["portfolio"]
    assert portfolio["asset_scope"] == {"symbol": "USDC"}
    assert portfolio["assets"] == []
    # A single-symbol lookup is not a portfolio-level card.
    assert "portfolio" not in result["response"]


class SymbolScopedBalanceWithBalanceModel:
    """A genuine single-coin balance question ("bnb 余额") that finds holdings
    must project the 余额/单链资产卡 (response.wallet + asset rows), not the
    multi-chain overview card and not prose-only."""

    def __init__(self):
        self.payload = None

    async def invoke_react(self, messages, tools):
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "scoped-balance-2",
                        "name": "wallet_balance",
                        "args": {"chain": "BSC", "symbol": "BNB", "address": WALLET},
                    }
                ],
            )
        self.payload = json.loads(tool_messages[0].content)
        return AIMessage(content="你 BSC 上的 BNB 余额是 0.5，约 384 美元。")


@pytest.mark.asyncio
async def test_react_symbol_scoped_balance_with_holdings_projects_wallet_card():
    from wallet_agent.graph.build import build_graph

    model = SymbolScopedBalanceWithBalanceModel()
    graph = build_graph(model=model, wallet_provider=WalletProvider())
    result = await graph.ainvoke(
        {
            "request": {"message": "bnb 余额", "address": WALLET, "chain": "BSC"},
            "wallet_context": {"address": WALLET, "chain": "BSC"},
            "conversation_history": [],
        },
        config={"configurable": {"thread_id": "react-scoped-balance-with-holdings"}},
    )

    assert result["response"]["kind"] == "react"
    assert model.payload is not None
    portfolio = model.payload["portfolio"]
    assert portfolio["asset_scope"] == {"symbol": "BNB"}
    assert portfolio["has_assets"] is True
    # The scoped balance is projected as the 余额/单链资产卡, not the overview.
    assert "portfolio" not in result["response"]
    assert isinstance(result["response"].get("wallet"), dict)
    assert result["response"]["asset_scope"] == {"symbol": "BNB"}
    assert len(result["response"].get("assets") or []) == 1
    row = result["response"]["assets"][0]
    assert row["asset"]["symbol"] == "BNB"
    assert row["asset"]["chain"] == "BSC"
    assert row["amount"] == "0.5"


class BalanceQueryPrepModel:
    def __init__(self):
        self.observation = None

    async def invoke_react(self, messages, tools):
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "prep-1",
                        "name": "prepare_transfer",
                        "args": {
                            "chain": "ARBITRUM",
                            "symbol": "ETH",
                            "amount": "0.00775626",
                            "recipient": WALLET,
                        },
                    }
                ],
            )
        self.observation = json.loads(tool_messages[0].content)
        return AIMessage(content="你的余额已展示在卡片里。")


@pytest.mark.asyncio
async def test_react_balance_query_does_not_prepare_stale_transfer():
    from wallet_agent.graph.build import build_graph

    model = BalanceQueryPrepModel()
    result = await build_graph(model=model).ainvoke(
        {
            "request": {"message": "查一下余额"},
            "wallet_context": {"address": WALLET, "chain": "ARBITRUM"},
            "conversation_history": [
                {"role": "user", "content": "转 0.00775626 ETH 给自己"},
                {"role": "assistant", "content": "收款地址已记录，请选网络。"},
            ],
            "transfer_draft": {
                "chain": "ARBITRUM",
                "symbol": "ETH",
                "amount": "0.00775626",
                "recipient": WALLET,
            },
        },
        config={"configurable": {"thread_id": "react-balance-no-prepare"}},
    )

    assert result["response"]["kind"] == "react"
    assert model.observation is not None
    assert model.observation["ok"] is False
    assert model.observation["code"] == "TRANSFER_NOT_REQUESTED"


class BalanceAfterTransferCardModel:
    """Answers a balance query with ``wallet_balance`` while a transfer card is
    still pending in the conversation."""

    def __init__(self):
        self.saw_balance = False

    async def invoke_react(self, messages, tools):
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "bal-1",
                        "name": "wallet_balance",
                        "args": {"chain": "BSC", "address": WALLET},
                    }
                ],
            )
        self.saw_balance = True
        return AIMessage(content="你的 BSC 余额约 384 美元（0.5 BNB）。")


@pytest.mark.asyncio
async def test_balance_query_after_transfer_card_does_not_render_transfer_step_card():
    """A balance question while a transfer card is pending must not also project
    the next transfer-collection card (coin/amount chips) next to the balance.

    Regression for the reported run: after the transfer card, the user asked
    "余额" and got a coin card next to the balance text, and selecting the coin
    then produced a transfer amount card. The transfer-collection fallback is
    gated on the model render decision, and a balance turn that produced a
    portfolio result suppresses that decision, so no transfer chips may appear.
    """
    from wallet_agent.graph.build import build_graph

    model = BalanceAfterTransferCardModel()
    graph = build_graph(model=model, wallet_provider=WalletProvider())
    result = await graph.ainvoke(
        {
            "request": {"message": "余额"},
            "wallet_context": {"address": WALLET, "chain": "BSC"},
            # The transfer card was already shown in an earlier turn: the
            # recipient and network are collected, and only the coin is left.
            # The graph rebuilds its message channel from this history, so the
            # transfer-collection slots are visible to the render fallback.
            "conversation_history": [
                {"role": "user", "content": "转 0.1 BNB 给我自己"},
                {"role": "assistant", "content": "好的，请提供收款地址。"},
                {"role": "user", "content": WALLET},
                {"role": "assistant", "content": "已记录收款地址，请选择网络。"},
                {"role": "user", "content": "BSC"},
                {"role": "assistant", "content": "BSC 网络已选定，请选择币种。"},
            ],
            "pending_transaction": {
                "chain": "BSC",
                "to": WALLET,
                "value": "100000000000000000",
            },
        },
        config={"configurable": {"thread_id": "react-balance-no-transfer-card"}},
    )

    response = result["response"]
    assert response["kind"] == "react"
    assert model.saw_balance is True
    suggestions = response.get("suggestions") or []
    suggestion_types = [str(item.get("type")) for item in suggestions]
    assert "symbol" not in suggestion_types
    assert "amount" not in suggestion_types
