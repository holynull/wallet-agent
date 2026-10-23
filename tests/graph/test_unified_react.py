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


def test_looks_like_balance_only_query_separates_read_from_transfer_steps():
    from wallet_agent.graph.nodes import _looks_like_balance_only_query

    assert _looks_like_balance_only_query("查一下余额") is True
    assert _looks_like_balance_only_query("我的资产总览") is True
    assert _looks_like_balance_only_query("看看 BNB 持仓价值") is True
    assert _looks_like_balance_only_query("用余额的 25% 转这笔") is False
    assert _looks_like_balance_only_query("继续转账 0.5 ETH") is False
    assert _looks_like_balance_only_query("转 0.5 ETH 给小明") is False


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
