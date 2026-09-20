from decimal import Decimal

import pytest

from wallet_agent.domain.models import (
    Asset,
    FeeEstimate,
    NormalizedQuote,
    SwapQuoteRequest,
    TokenBalance,
    TokenPrice,
    UnsignedTransaction,
)
from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import (
    GraphRuntime,
    _resolve_swap_assets,
    _swap_draft_request,
    make_nodes,
)


class FakeModel:
    async def ainvoke(self, value):
        return {"intent": value.get("intent", "clarification")}


class SwapExtractionModel:
    def __init__(self, output):
        self.output = output

    async def ainvoke(self, _value):
        return self.output


class SequentialSwapExtractionModel:
    def __init__(self, outputs):
        self.outputs = iter(outputs)

    async def ainvoke(self, _value):
        return next(self.outputs)


class FakeProvider:
    def __init__(self, name: str, *, price_snapshots: dict[str, TokenPrice] | None = None):
        self.provider_name = name
        self.calls = []
        self.price_snapshots = price_snapshots or {}

    async def quote(self, request):
        self.calls.append(("quote", request))
        return NormalizedQuote(
            provider=self.provider_name,
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal("9"),
            expected_output_raw="9000000",
            provider_reference=f"{self.provider_name}-ref",
            price_snapshots=self.price_snapshots,
        )

    async def prepare(self, quote):
        self.calls.append(("prepare", quote))
        return UnsignedTransaction(
            chain=quote.source_asset.chain,
            to="0xrouter",
            data="0xdata",
            value="0x0",
            provider=self.provider_name,
            provider_reference=quote.provider_reference,
        )


class ZeroOutputProvider(FakeProvider):
    async def quote(self, request):
        quote = await super().quote(request)
        return quote.model_copy(
            update={"expected_output": Decimal("0"), "expected_output_raw": "0"}
        )


def quote_request():
    return SwapQuoteRequest(
        source_asset=Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x1"),
        destination_asset=Asset(chain="BSC", chain_id=56, symbol="USDT", decimals=6, address="0x2"),
        input_amount=Decimal("10"),
        input_amount_raw="10000000",
        sender_address="0xsender",
        recipient_address="0xrecipient",
    )


@pytest.mark.asyncio
async def test_quote_path_fans_out_and_reduces_candidates():
    graph = build_graph(
        model=FakeModel(),
        providers=[FakeProvider("bridgers"), FakeProvider("omnibridge")],
        price_provider=object(),
    )
    result = await graph.ainvoke(
        {
            "conversation_id": "c1",
            "user_id": "u1",
            "intent": "swap_quote",
            "swap_request": quote_request(),
        },
        config={"configurable": {"thread_id": "t-1"}},
    )
    assert {quote["provider"] for quote in result["quote_candidates"]} == {"bridgers", "omnibridge"}


@pytest.mark.asyncio
async def test_quote_path_reports_missing_provider_instead_of_empty_success():
    graph = build_graph(model=FakeModel(), providers=[])
    result = await graph.ainvoke(
        {
            "conversation_id": "c-no-provider",
            "user_id": "u1",
            "intent": "swap_quote",
            "swap_request": quote_request(),
        },
        config={"configurable": {"thread_id": "t-no-provider"}},
    )
    assert result["response"]["kind"] == "error"
    assert result["response"]["errors"][0]["code"] == "PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_quote_path_rejects_zero_output_quote_before_confirmation():
    graph = build_graph(
        model=FakeModel(),
        providers=[ZeroOutputProvider("bridgers")],
    )
    result = await graph.ainvoke(
        {
            "conversation_id": "c-zero-output",
            "user_id": "u1",
            "intent": "swap_quote",
            "swap_request": quote_request(),
        },
        config={"configurable": {"thread_id": "t-zero-output"}},
    )
    assert result["response"]["kind"] == "error"
    assert result["response"]["errors"][0]["code"] == "INVALID_QUOTE"
    assert result["quote_candidates"] == []
    assert result["selected_quote"] is None


@pytest.mark.asyncio
async def test_confirmation_includes_slippage_and_read_only_gas_estimate():
    class GasAdapter:
        async def estimate_fee(self, *, to=None, data=None):
            del to, data
            return FeeEstimate(
                chain="BASE",
                chain_id=8453,
                asset=Asset(chain="BASE", chain_id=8453, symbol="ETH", decimals=18),
                amount=Decimal("0.001"),
                amount_raw="1000000000000000",
                gas_limit="21000",
            )

    quote = FakeProvider("bridgers")
    selected = (await quote.quote(quote_request())).model_dump(mode="json")
    nodes = make_nodes(
        GraphRuntime(model=FakeModel(), providers={}, chains={"BASE": GasAdapter()})
    )

    result = await nodes["confirmation_request"](
        {
            "conversation_id": "gas-confirmation",
            "selected_quote": selected,
            "active_task": {
                "task_id": "task-1",
                "kind": "swap",
                "revision": 1,
                "slots": {"slippage_bps": 100},
            },
        }
    )

    assert result["response"]["kind"] == "confirmation_required"
    assert result["response"]["gas_estimate"]["amount_raw"] == "1000000000000000"
    assert result["response"]["confirmation"]["summary"]["slippage_bps"] == 100


@pytest.mark.asyncio
async def test_quote_path_rejects_same_asset_before_calling_provider():
    provider = FakeProvider("bridgers")
    request = quote_request()
    same_asset = request.source_asset.model_copy()
    same_request = request.model_copy(update={"destination_asset": same_asset})
    graph = build_graph(model=FakeModel(), providers=[provider])

    result = await graph.ainvoke(
        {
            "conversation_id": "c-same-asset",
            "user_id": "u1",
            "intent": "swap_quote",
            "swap_request": same_request,
        },
        config={"configurable": {"thread_id": "t-same-asset"}},
    )

    assert result["response"]["kind"] == "error"
    assert result["response"]["errors"][0]["code"] == "SOURCE_EQUALS_DESTINATION"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_quote_path_filters_invalid_provider_without_losing_valid_candidate():
    graph = build_graph(
        model=FakeModel(),
        providers=[ZeroOutputProvider("bridgers"), FakeProvider("omnibridge")],
    )
    result = await graph.ainvoke(
        {
            "conversation_id": "c-mixed-quotes",
            "user_id": "u1",
            "intent": "swap_quote",
            "swap_request": quote_request(),
        },
        config={"configurable": {"thread_id": "t-mixed-quotes"}},
    )
    assert result["response"]["kind"] == "swap_quote"
    assert [quote["provider"] for quote in result["quote_candidates"]] == ["omnibridge"]


@pytest.mark.asyncio
async def test_resolve_swap_clears_stale_response_before_quote_fanout():
    provider = FakeProvider("bridgers")
    runtime = GraphRuntime(model=FakeModel(), providers={"bridgers": provider}, chains={})
    resolve_swap = make_nodes(runtime)["resolve_swap"]
    result = await resolve_swap(
        {
            "intent": "swap_quote",
            "swap_request": quote_request().model_dump(mode="json"),
            "response": {
                "kind": "clarification",
                "message": "还需要确认来源链和目标链。",
            },
        }
    )
    assert result["response"] is None


@pytest.mark.asyncio
async def test_swap_asset_resolution_accepts_native_destination_without_contract_address():
    resolved, candidates, errors = await _resolve_swap_assets(
        {
            "source_chain": "ETH",
            "source_symbol": "USDT",
            "source_token_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
            "source_decimals": 6,
            "destination_chain": "BSC",
            "destination_symbol": "BNB",
            "input_amount": "10",
        },
        {},
    )

    assert candidates == []
    assert errors == []
    assert resolved["destination_decimals"] == 18
    assert resolved["destination_chain_id"] == 56
    assert resolved.get("destination_token_address") is None

    request, missing = _swap_draft_request(
        resolved,
        {"address": "0x" + "1" * 40, "chain": "ETH", "chain_id": 1},
    )
    assert missing == []
    assert request is not None
    assert request.destination_asset.symbol == "BNB"
    assert request.destination_asset.address is None


@pytest.mark.asyncio
async def test_swap_asset_resolution_accepts_native_source_without_contract_address():
    resolved, candidates, errors = await _resolve_swap_assets(
        {
            "source_chain": "BSC",
            "source_symbol": "BNB",
            "destination_chain": "ETH",
            "destination_symbol": "USDT",
            "destination_token_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
            "destination_decimals": 6,
            "input_amount": "0.1",
        },
        {},
    )

    assert candidates == []
    assert errors == []
    assert resolved["source_decimals"] == 18
    assert resolved["source_chain_id"] == 56
    request, missing = _swap_draft_request(
        resolved,
        {"address": "0x" + "1" * 40, "chain": "BSC", "chain_id": 56},
    )
    assert missing == []
    assert request is not None
    assert request.input_amount_raw == "100000000000000000"
    assert request.source_asset.address is None


@pytest.mark.asyncio
async def test_swap_asset_resolution_does_not_treat_unknown_symbol_as_native():
    resolved, candidates, errors = await _resolve_swap_assets(
        {
            "source_chain": "BSC",
            "source_symbol": "NOTBNB",
            "destination_chain": "BSC",
            "destination_symbol": "BNB",
            "input_amount": "1",
        },
        {},
    )

    assert resolved["destination_decimals"] == 18
    assert candidates == []
    assert errors[0]["code"] == "ASSET_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_quote_path_preserves_all_candidates_and_price_snapshots_without_selection():
    source = Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x1")
    destination = Asset(chain="BSC", chain_id=56, symbol="USDT", decimals=6, address="0x2")
    graph = build_graph(
        model=FakeModel(),
        providers=[
            FakeProvider(
                "bridgers",
                price_snapshots={
                    "BASE:8453:USDC:0x1": TokenPrice(asset=source, usd_price=Decimal("1"))
                },
            ),
            FakeProvider(
                "omnibridge",
                price_snapshots={
                    "BSC:56:USDT:0x2": TokenPrice(asset=destination, usd_price=Decimal("1"))
                },
            ),
        ],
    )
    result = await graph.ainvoke(
        {
            "conversation_id": "c1",
            "user_id": "u1",
            "intent": "swap_quote",
            "swap_request": quote_request(),
        },
        config={"configurable": {"thread_id": "t-2"}},
    )
    assert [quote["provider"] for quote in result["quote_candidates"]] == [
        "bridgers",
        "omnibridge",
    ]
    assert result["selected_quote"] is None
    assert result["response"]["kind"] == "swap_quote"
    assert result["response"]["price_snapshots"]["BASE:8453:USDC:0x1"]["usd_price"] == "1"
    assert result["response"]["price_snapshots"]["BSC:56:USDT:0x2"]["usd_price"] == "1"


@pytest.mark.asyncio
async def test_malformed_model_output_routes_to_clarification():
    class BadModel:
        async def ainvoke(self, value):
            return "not structured"

    graph = build_graph(model=BadModel(), providers=[], price_provider=object())
    result = await graph.ainvoke(
        {"conversation_id": "c1", "user_id": "u1", "message": "swap"},
        config={"configurable": {"thread_id": "bad"}},
    )
    assert result["intent"] == "clarification"
    assert result["response"]["kind"] == "clarification"


@pytest.mark.asyncio
async def test_greeting_returns_a_helpful_clarification_message():
    graph = build_graph(
        model=SwapExtractionModel({"intent": "clarification"}),
        providers=[],
        price_provider=object(),
    )
    result = await graph.ainvoke(
        {"conversation_id": "c-greeting", "user_id": "u1", "message": "你好"},
        config={"configurable": {"thread_id": "greeting"}},
    )
    assert result["response"]["kind"] == "clarification"
    assert result["response"]["message"].startswith("你好！")


@pytest.mark.asyncio
async def test_conversational_swap_reports_missing_asset_catalog_without_calling_quote():
    provider = FakeProvider("bridgers")
    graph = build_graph(
        model=SwapExtractionModel(
            {
                "intent": "swap_quote",
                "source_chain": "BASE",
                "destination_chain": "BSC",
                "source_symbol": "USDC",
                "destination_symbol": "USDT",
                "input_amount": "1",
            }
        ),
        providers=[provider],
    )
    result = await graph.ainvoke(
        {
            "conversation_id": "chat-1",
            "user_id": "u1",
            "message": "把 1 USDC 换成 USDT",
            "wallet_context": {"address": "0x" + "1" * 40, "chain": "BASE", "chain_id": 8453},
        },
        config={"configurable": {"thread_id": "chat-1"}},
    )
    assert result["response"]["kind"] == "error"
    assert result["response"]["errors"][0]["code"] == "ASSET_PROVIDER_UNAVAILABLE"
    assert result["missing_fields"] == []
    assert provider.calls == []


@pytest.mark.asyncio
async def test_conversational_swap_uses_wallet_address_and_enters_quote_path():
    provider = FakeProvider("bridgers")
    graph = build_graph(
        model=SwapExtractionModel(
            {
                "intent": "swap_quote",
                "source_chain": "BASE",
                "destination_chain": "BSC",
                "source_symbol": "USDC",
                "destination_symbol": "USDT",
                "source_token_address": "0x" + "2" * 40,
                "destination_token_address": "0x" + "3" * 40,
                "source_decimals": 6,
                "destination_decimals": 6,
                "input_amount": "1",
            }
        ),
        providers=[provider],
    )
    result = await graph.ainvoke(
        {
            "conversation_id": "chat-2",
            "user_id": "u1",
            "message": "把 1 USDC 换成 USDT",
            "wallet_context": {"address": "0x" + "1" * 40, "chain": "BASE", "chain_id": 8453},
        },
        config={"configurable": {"thread_id": "chat-2"}},
    )
    assert result["response"]["kind"] == "swap_quote"
    assert result["swap_request"]["sender_address"] == "0x" + "1" * 40
    assert result["swap_request"]["recipient_address"] == "0x" + "1" * 40
    assert result["swap_request"]["input_amount_raw"] == "1000000"
    assert len(result["quote_candidates"]) == 1


@pytest.mark.asyncio
async def test_conversational_swap_merges_draft_across_turns():
    provider = FakeProvider("bridgers")
    graph = build_graph(
        model=SequentialSwapExtractionModel(
            [
                {
                    "intent": "swap_quote",
                    "source_chain": "BASE",
                    "destination_chain": "BSC",
                    "source_symbol": "USDC",
                    "destination_symbol": "USDT",
                    "input_amount": "1",
                },
                {
                    "intent": "swap_quote",
                    "source_token_address": "0x" + "2" * 40,
                    "destination_token_address": "0x" + "3" * 40,
                    "source_decimals": 6,
                    "destination_decimals": 6,
                },
            ]
        ),
        providers=[provider],
    )
    config = {"configurable": {"thread_id": "chat-3"}}
    wallet_context = {
        "address": "0x" + "1" * 40,
        "chain": "BASE",
        "chain_id": 8453,
    }

    first = await graph.ainvoke(
        {
            "conversation_id": "chat-3",
            "user_id": "u1",
            "request": {"message": "把 1 USDC 换成 USDT"},
            "wallet_context": wallet_context,
        },
        config=config,
    )
    second = await graph.ainvoke(
        {
            "conversation_id": "chat-3",
            "user_id": "u1",
            "request": {"message": "源 token 和目标 token 地址分别是已提供的地址"},
        },
        config=config,
    )

    assert first["response"]["kind"] == "error"
    assert first["response"]["errors"][0]["code"] == "ASSET_PROVIDER_UNAVAILABLE"
    assert second["response"]["kind"] == "swap_quote"
    assert second["swap_draft"]["source_symbol"] == "USDC"
    assert second["swap_request"]["source_asset"]["address"] == "0x" + "2" * 40
    assert second["swap_request"]["sender_address"] == wallet_context["address"]
    assert provider.calls


class TransferAdapter:
    def __init__(
        self,
        *,
        native_raw="2000000000000000000",
        token_raw="2000000",
        fee_raw="1000000000000000",
    ):
        self.native_raw = native_raw
        self.token_raw = token_raw
        self.fee_raw = fee_raw

    async def validate_address(self, address):
        return address.startswith("0x") and len(address) == 42

    async def get_native_balance(self, _address):
        asset = Asset(chain="BASE", symbol="ETH", decimals=18)
        return TokenBalance(
            asset=asset,
            amount=Decimal(self.native_raw) / Decimal(10**18),
            amount_raw=self.native_raw,
        )

    async def estimate_fee(self, *, to=None, data=None):
        asset = Asset(chain="BASE", symbol="ETH", decimals=18)
        return FeeEstimate(
            chain="BASE",
            chain_id=8453,
            asset=asset,
            amount=Decimal(self.fee_raw) / Decimal(10**18),
            amount_raw=self.fee_raw,
            gas_limit="21000",
        )

    async def get_token_balance(self, _token, _address):
        asset = Asset(chain="BASE", symbol="USDC", decimals=6, address="0x" + "3" * 40)
        return TokenBalance(
            asset=asset,
            amount=Decimal(self.token_raw) / Decimal(10**6),
            amount_raw=self.token_raw,
        )

    def build_native_transfer(self, *, from_address, to_address, amount_raw):
        return UnsignedTransaction(
            chain="BASE",
            chain_id=8453,
            to=to_address,
            data="0x",
            value=amount_raw,
        )

    def build_erc20_transfer(self, *, token, from_address, to_address, amount_raw):
        return UnsignedTransaction(
            chain=token.chain,
            chain_id=8453,
            to=token.address,
            data="0xa9059cbb",
            value="0",
        )

    async def get_transaction_status(self, _tx_hash):
        from wallet_agent.domain.models import TransactionStatus

        return TransactionStatus.CONFIRMED

    async def get_token_balances(self, _address):
        return []


@pytest.mark.asyncio
async def test_conversational_transfer_extracts_fields_and_returns_preflight():
    graph = build_graph(
        model=SwapExtractionModel(
            {
                "intent": "transfer",
                "transfer_chain": "BASE",
                "transfer_symbol": "ETH",
                "transfer_amount": "1",
                "transfer_recipient": "0x" + "2" * 40,
            }
        ),
        chains={"BASE": TransferAdapter()},
    )
    result = await graph.ainvoke(
        {
            "conversation_id": "transfer-1",
            "user_id": "u1",
            "message": "转 1 ETH 给这个地址",
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "BASE",
                "chain_id": 8453,
            },
        },
        config={"configurable": {"thread_id": "transfer-1"}},
    )
    assert result["response"]["kind"] == "transfer_prepare"
    assert result["transfer_request"]["sender"] == "0x" + "1" * 40
    assert result["preflight"]["ok"] is True
    assert result["preflight"]["fee_estimate"]["amount_raw"] == "1000000000000000"


@pytest.mark.asyncio
async def test_conversational_eth_usdc_transfer_resolves_common_token_metadata():
    graph = build_graph(
        model=SwapExtractionModel(
            {
                "intent": "transfer",
                "transfer_symbol": "USDC",
                "transfer_amount": "1",
                "transfer_recipient": "0x" + "2" * 40,
            }
        ),
        chains={"ETH": TransferAdapter()},
    )
    result = await graph.ainvoke(
        {
            "conversation_id": "transfer-eth-usdc",
            "user_id": "u1",
            "message": "给地址转 1 USDC",
            "wallet_context": {
                "address": "0x" + "1" * 40,
                "chain": "ETH",
                "chain_id": 1,
            },
        },
        config={"configurable": {"thread_id": "transfer-eth-usdc"}},
    )

    assert result["response"]["kind"] == "transfer_prepare"
    assert result["transfer_request"]["token"]["address"] == (
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    )
    assert result["transfer_request"]["token"]["decimals"] == 6
    assert result["transfer_request"]["amount_raw"] == "1000000"


@pytest.mark.asyncio
async def test_conversational_transfer_asks_for_missing_recipient():
    graph = build_graph(
        model=SwapExtractionModel(
            {
                "intent": "transfer",
                "transfer_chain": "BASE",
                "transfer_symbol": "ETH",
                "transfer_amount": "1",
            }
        ),
        chains={"BASE": TransferAdapter()},
    )
    result = await graph.ainvoke(
        {
            "conversation_id": "transfer-2",
            "user_id": "u1",
            "message": "转 1 ETH",
            "wallet_context": {"address": "0x" + "1" * 40, "chain": "BASE"},
        },
        config={"configurable": {"thread_id": "transfer-2"}},
    )
    assert result["response"]["kind"] == "clarification"
    assert "transfer_recipient" in result["response"]["missing_fields"]


@pytest.mark.asyncio
async def test_transaction_status_node_returns_human_readable_result():
    graph = build_graph(model=FakeModel(), chains={"BASE": TransferAdapter()})
    result = await graph.ainvoke(
        {
            "conversation_id": "tx-1",
            "user_id": "u1",
            "intent": "transaction_status",
            "transaction_query": {
                "chain": "BASE",
                "tx_hash": "0x" + "a" * 64,
            },
        },
        config={"configurable": {"thread_id": "tx-1"}},
    )
    assert result["response"]["kind"] == "transaction_status"
    assert result["transaction_status_snapshot"]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_transfer_preflight_blocks_when_native_amount_and_fee_exceed_balance():
    graph = build_graph(
        model=FakeModel(),
        chains={
            "BASE": TransferAdapter(
                native_raw="1000000000000000000",
                fee_raw="100000000000000000",
            )
        },
    )
    result = await graph.ainvoke(
        {
            "intent": "transfer",
            "transfer_request": {
                "chain": "BASE",
                "sender": "0x" + "1" * 40,
                "recipient": "0x" + "2" * 40,
                "amount": "1",
                "amount_raw": "1000000000000000000",
            },
        },
        config={"configurable": {"thread_id": "transfer-insufficient-native"}},
    )
    assert result["response"]["kind"] == "error"
    assert result["preflight"]["ok"] is False
    assert any(
        item.get("code") == "INSUFFICIENT_BALANCE" for item in result["preflight"]["checks"]
    )


@pytest.mark.asyncio
async def test_token_transfer_preflight_blocks_when_native_gas_is_insufficient():
    graph = build_graph(
        model=FakeModel(),
        chains={"BASE": TransferAdapter(native_raw="1", token_raw="2000000", fee_raw="100")},
    )
    result = await graph.ainvoke(
        {
            "intent": "transfer",
            "transfer_request": {
                "chain": "BASE",
                "sender": "0x" + "1" * 40,
                "recipient": "0x" + "2" * 40,
                "amount": "1",
                "amount_raw": "1000000",
                "token": {
                    "chain": "BASE",
                    "symbol": "USDC",
                    "decimals": 6,
                    "address": "0x" + "3" * 40,
                },
            },
        },
        config={"configurable": {"thread_id": "transfer-insufficient-gas"}},
    )
    assert result["response"]["kind"] == "error"
    assert any(
        item.get("code") == "INSUFFICIENT_GAS" for item in result["preflight"]["checks"]
    )


@pytest.mark.asyncio
async def test_transfer_preflight_blocks_when_wallet_account_does_not_match_sender():
    graph = build_graph(model=FakeModel(), chains={"BASE": TransferAdapter()})
    result = await graph.ainvoke(
        {
            "intent": "transfer",
            "transfer_request": {
                "chain": "BASE",
                "sender": "0x" + "1" * 40,
                "recipient": "0x" + "2" * 40,
                "amount": "1",
                "amount_raw": "1000000000000000000",
            },
            "wallet_context": {
                "address": "0x" + "9" * 40,
                "chain": "BASE",
            },
        },
        config={"configurable": {"thread_id": "transfer-account-mismatch"}},
    )
    assert result["response"]["kind"] == "error"
    assert any(
        item["code"] == "WALLET_ACCOUNT_MISMATCH" for item in result["preflight"]["checks"]
    )


class PortfolioPriceProvider:
    async def get_prices(self, assets):
        return [
            TokenPrice(asset=asset, usd_price=Decimal("2"))
            for asset in assets
        ]


class AssetProvider:
    provider_name = "bridgers"

    async def list_assets(self, query):
        return [
            Asset(
                chain=query.chain or "BASE",
                symbol="USDC",
                decimals=6,
                address="0x" + "3" * 40,
            ),
            Asset(
                chain=query.chain or "BASE",
                symbol="USDC",
                decimals=6,
                address="0x" + "3" * 40,
            ),
        ]


@pytest.mark.asyncio
async def test_portfolio_query_returns_balances_and_usd_snapshot():
    graph = build_graph(
        model=FakeModel(),
        chains={"BASE": TransferAdapter()},
        price_provider=PortfolioPriceProvider(),
    )
    result = await graph.ainvoke(
        {
            "intent": "portfolio_query",
            "portfolio_request": {
                "chain": "BASE",
                "address": "0x" + "1" * 40,
            },
        },
        config={"configurable": {"thread_id": "portfolio-1"}},
    )
    assert result["response"]["kind"] == "portfolio_query"
    assert result["portfolio_snapshot"]["price_status"] == "available"
    assert result["portfolio_snapshot"]["total_usd_value"] == "4"


@pytest.mark.asyncio
async def test_gas_check_reports_native_balance_shortfall():
    graph = build_graph(
        model=FakeModel(),
        chains={
            "BASE": TransferAdapter(
                native_raw="1",
                fee_raw="100",
            )
        },
    )
    result = await graph.ainvoke(
        {
            "intent": "gas_check",
            "gas_request": {
                "chain": "BASE",
                "address": "0x" + "1" * 40,
            },
        },
        config={"configurable": {"thread_id": "gas-1"}},
    )
    assert result["response"]["kind"] == "gas_check"
    assert result["gas_snapshot"]["sufficient"] is False
    assert result["gas_snapshot"]["shortfall_raw"] == "99"


@pytest.mark.asyncio
async def test_asset_discovery_merges_provider_results():
    graph = build_graph(model=FakeModel(), providers=[AssetProvider()])
    result = await graph.ainvoke(
        {
            "intent": "asset_discovery",
            "asset_query": {"chain": "BASE", "search": "USDC"},
        },
        config={"configurable": {"thread_id": "assets-1"}},
    )
    assert result["response"]["kind"] == "asset_discovery"
    assert len(result["asset_snapshot"]["assets"]) == 1
    assert result["asset_snapshot"]["assets"][0]["symbol"] == "USDC"
