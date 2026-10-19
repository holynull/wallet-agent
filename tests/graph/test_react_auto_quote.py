"""A complete swap request defaults to one quote and continues to confirmation.

App-side report: once coin/amount/chain are complete, the backend asked the user
to pick a quote channel, while the design (Figma) renders the confirmation card
straight away with a default channel. The ReAct flow now deterministically picks
the best quote (``selected_quote``) so the API continues into confirmation
instead of surfacing the channel picker.
"""

from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.domain.models import Asset, AssetQuery, NormalizedQuote

WALLET = "0x" + "1" * 40


class SwapProvider:
    def __init__(self, name, output):
        self.provider_name = name
        self.output = output

    async def list_assets(self, query):
        assert isinstance(query, AssetQuery)
        if query.search == "USDC":
            return [Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x1")]
        if query.search == "USDT":
            return [Asset(chain="BASE", chain_id=8453, symbol="USDT", decimals=6, address="0x2")]
        return []

    async def quote(self, request):
        return NormalizedQuote(
            provider=self.provider_name,
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal(self.output),
            expected_output_raw=str(int(Decimal(self.output) * Decimal(10) ** 6)),
            provider_reference=f"quote-{self.provider_name}",
        )


class QuoteModel:
    async def invoke_react(self, messages, tools):
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "quote-1",
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
        return AIMessage(content="报价已就绪。")


def _run(model, providers):
    from wallet_agent.graph.build import build_graph

    return build_graph(model=model, providers=providers).ainvoke(
        {
            "request": {"message": "在 Base 把 1 USDC 换成 USDT"},
            "wallet_context": {"address": WALLET, "chain": "BASE"},
        },
        config={"configurable": {"thread_id": "react-auto-quote"}},
    )


@pytest.mark.asyncio
async def test_default_quote_picks_highest_expected_output():
    """Exact-in swap defaults to the provider with the best output."""
    from wallet_agent.graph.nodes import _default_quote

    request = {"amount_mode": "exact_in", "input_amount": "1"}
    quotes = [
        {
            "provider": "a",
            "provider_reference": "q-a",
            "input_amount": "1",
            "expected_output": "0.98",
        },
        {
            "provider": "b",
            "provider_reference": "q-b",
            "input_amount": "1",
            "expected_output": "0.99",
        },
    ]
    assert _default_quote(quotes, request)["provider_reference"] == "q-b"


@pytest.mark.asyncio
async def test_default_quote_exact_out_picks_lowest_input():
    """Exact-out swap defaults to the provider requiring the least input."""
    from wallet_agent.graph.nodes import _default_quote

    request = {"amount_mode": "exact_out", "output_amount": "100"}
    quotes = [
        {
            "provider": "a",
            "provider_reference": "q-a",
            "input_amount": "99",
            "expected_output": "100",
        },
        {
            "provider": "b",
            "provider_reference": "q-b",
            "input_amount": "98",
            "expected_output": "100",
        },
    ]
    assert _default_quote(quotes, request)["provider_reference"] == "q-b"


@pytest.mark.asyncio
async def test_react_quote_sets_default_selected_quote():
    """A complete swap quote auto-selects the best channel on the react turn."""
    model = QuoteModel()
    result = await _run(
        model,
        providers=[
            SwapProvider("bridgers", "0.98"),
            SwapProvider("omnibridge", "0.99"),
        ],
    )

    assert result["response"]["kind"] == "react"
    assert result["quote_candidates"]
    assert result["swap_request"]["source_asset"]["symbol"] == "USDC"
    selected = result["selected_quote"]
    assert selected is not None
    assert selected["provider"] == "omnibridge"
    assert selected["provider_reference"] == "quote-omnibridge"
    # Candidates stay available for audit / explicit reselection.
    assert {item["provider"] for item in result["quote_candidates"]} == {"bridgers", "omnibridge"}


class CrossChainSwapProvider:
    """ETH USDT -> TRON TRX bridge-style provider (TRX is a native asset)."""

    def __init__(self, name, output):
        self.provider_name = name
        self.output = output

    async def list_assets(self, query):
        assert isinstance(query, AssetQuery)
        if query.search == "USDT" and query.chain == "ETH":
            return [
                Asset(
                    chain="ETH",
                    chain_id=1,
                    symbol="USDT",
                    decimals=6,
                    address="0xdac17f958d2ee523a2206206994597c13d831ec7",
                )
            ]
        return []

    async def quote(self, request):
        return NormalizedQuote(
            provider=self.provider_name,
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal(self.output),
            expected_output_raw=str(int(Decimal(self.output) * Decimal(10) ** 6)),
            provider_reference=f"quote-{self.provider_name}",
        )


class WrongSourceChainQuoteModel:
    """The model guesses TRON as the source chain for a TRX target.

    Lif's real session 20e62b6a (“用eth的usdt换trx”) hit this exact guess and
    surfaced ASSET_NOT_FOUND / 报价不存在. The tool must prefer the user's own
    words (source chain ETH) over the model's mirror of the target chain.
    """

    async def invoke_react(self, messages, tools):
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "quote-1",
                        "name": "get_swap_quote",
                        "args": {
                            "source_symbol": "USDT",
                            "destination_symbol": "TRX",
                            "input_amount": "10",
                            "source_chain": "TRON",
                            "destination_chain": "TRON",
                        },
                    }
                ],
            )
        return AIMessage(content="报价已就绪。")


def _run_eth_usdt_trx(model, providers):
    from wallet_agent.graph.build import build_graph

    return build_graph(model=model, providers=providers).ainvoke(
        {
            "request": {"message": "用eth的usdt换trx"},
            "wallet_context": {"address": WALLET, "chain": "ETH"},
        },
        config={"configurable": {"thread_id": "react-cross-chain-direction"}},
    )


@pytest.mark.asyncio
async def test_quote_uses_user_named_source_chain_over_model_guess():
    """A model-guessed TRON source is corrected to the user's ETH for the quote."""
    result = await _run_eth_usdt_trx(
        WrongSourceChainQuoteModel(),
        providers=[
            CrossChainSwapProvider("bridgers", "29.10"),
            CrossChainSwapProvider("omnibridge", "29.10"),
        ],
    )

    assert result["quote_candidates"], result.get("errors")
    for candidate in result["quote_candidates"]:
        source = candidate.get("source_asset") or {}
        assert source.get("chain") == "ETH"
        assert source.get("symbol") == "USDT"
