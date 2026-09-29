from decimal import Decimal

import pytest

from wallet_agent.domain.models import (
    Asset,
    AssetQuery,
    FeeEstimate,
    NormalizedQuote,
    ProviderOrder,
    TokenBalance,
    TokenPrice,
    UnsignedTransaction,
)
from wallet_agent.graph import build_graph
from wallet_agent.models import RouteDecision, SwapSlotPatch, TransferSlotPatch
from wallet_agent.persistence import create_checkpointer

WALLET = "0x" + "1" * 40
RECIPIENT = "0x" + "2" * 40
USDC = "0x" + "3" * 40
USDT = "0x" + "4" * 40


class UnderstandingModel:
    def __init__(self, classifications, *, transfer=(), swap=()):
        self.classifications = iter(classifications)
        self.patches = {"transfer": iter(transfer), "swap": iter(swap)}
        self.extracted_kinds = []

    async def classify(self, _request):
        return RouteDecision(intent=next(self.classifications))

    async def extract(self, task_kind, _request):
        self.extracted_kinds.append(task_kind)
        return next(self.patches[task_kind])


class TransactionStatusModel:
    def __init__(self, patches):
        self.patches = iter(patches)

    async def classify(self, _request):
        return RouteDecision(intent="transaction_status")

    async def extract(self, task_kind, _request):
        assert task_kind == "transaction_status"
        patch = next(self.patches)
        if isinstance(patch, Exception):
            raise patch
        return patch


class Provider:
    provider_name = "bridgers"

    def __init__(self):
        self.quote_calls = 0
        self.last_slippage_bps = None

    async def list_assets(self, query: AssetQuery):
        assets = {
            "USDC": Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address=USDC),
            "USDT": Asset(chain="BASE", chain_id=8453, symbol="USDT", decimals=6, address=USDT),
        }
        asset = assets.get(str(query.search).upper())
        return [asset] if asset and str(query.chain).upper() == "BASE" else []

    async def quote(self, request):
        self.quote_calls += 1
        self.last_slippage_bps = request.slippage_bps
        return NormalizedQuote(
            provider="bridgers",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=request.input_amount,
            expected_output_raw=request.input_amount_raw,
            provider_reference=f"quote-{self.quote_calls}",
        )

    async def reverse_quote(self, request, output_amount):
        self.quote_calls += 1
        input_amount = output_amount * 2
        return NormalizedQuote(
            provider="bridgers",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=input_amount,
            input_amount_raw=str(int(input_amount * Decimal(10**request.source_asset.decimals))),
            expected_output=output_amount,
            expected_output_raw=str(
                int(output_amount * Decimal(10**request.destination_asset.decimals))
            ),
            provider_reference=f"reverse-{self.quote_calls}",
        )


class FixedPrices:
    async def get_prices(self, assets):
        values = {"BNB": Decimal("500"), "ETH": Decimal("2000")}
        return [
            TokenPrice(asset=asset, usd_price=values[asset.symbol], provider="okx")
            for asset in assets
            if asset.symbol in values
        ]


class EthereumCatalogProvider(Provider):
    async def list_assets(self, query: AssetQuery):
        assert query.chain == "ETH"
        assets = {
            "USDC": Asset(
                chain="ETH",
                symbol="USDC",
                decimals=6,
                address=USDC,
                name="USD Coin",
                logo_url="https://example.invalid/usdc.png",
            ),
            "USDT": Asset(
                chain="ETH",
                symbol="USDT(ERC20)",
                decimals=6,
                address=USDT,
                name="Tether",
                logo_url="https://example.invalid/usdt.png",
            ),
        }
        asset = assets.get(str(query.search).upper())
        return [asset] if asset else []


class Chain:
    async def validate_address(self, address):
        return address.startswith("0x") and len(address) == 42

    async def get_native_balance(self, _address):
        return TokenBalance(
            asset=Asset(chain="BASE", chain_id=8453, symbol="ETH", decimals=18),
            amount=Decimal("2"),
            amount_raw="2000000000000000000",
        )

    async def get_token_balances(self, _address):
        return []

    async def estimate_fee(self, *, to=None, data=None):
        del to, data
        return FeeEstimate(
            chain="BASE",
            chain_id=8453,
            asset=Asset(chain="BASE", chain_id=8453, symbol="ETH", decimals=18),
            amount=Decimal("0.0001"),
            amount_raw="100000000000000",
        )

    def build_native_transfer(self, *, from_address, to_address, amount_raw):
        del from_address
        return UnsignedTransaction(
            chain="BASE", chain_id=8453, to=to_address, data="0x", value=amount_raw
        )


class BscTokenChain:
    async def validate_address(self, address):
        return address.startswith("0x") and len(address) == 42

    async def get_native_balance(self, _address):
        return TokenBalance(
            asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
            amount=Decimal("1"),
            amount_raw="1000000000000000000",
        )

    async def get_token_balance(self, token, _address):
        return TokenBalance(
            asset=token,
            amount=Decimal("10"),
            amount_raw="10000000000000000000",
        )

    async def estimate_fee(self, *, to=None, data=None):
        del to, data
        return FeeEstimate(
            chain="BSC",
            chain_id=56,
            asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
            amount=Decimal("0.0001"),
            amount_raw="100000000000000",
        )

    def build_erc20_transfer(self, *, token, from_address, to_address, amount_raw):
        del from_address, to_address
        return UnsignedTransaction(
            chain="BSC",
            chain_id=56,
            to=token.address,
            data="0xa9059cbb",
            value="0",
        )


class BscWalletProvider:
    chain_index_by_name = {"BSC": "56"}

    async def get_token_balances(self, _address, chain_indexes):
        assert chain_indexes == ["56"]
        return [
            TokenBalance(
                asset=Asset(
                    chain="BSC",
                    chain_id=56,
                    symbol="USDC",
                    decimals=18,
                    address="0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
                ),
                amount=Decimal("8"),
                amount_raw="8000000000000000000",
            )
        ]


class OkxWalletProvider:
    chain_index_by_name = {"BASE": "8453"}

    async def get_token_balances(self, _address, chain_indexes):
        assert chain_indexes == ["8453"]
        return [
            TokenBalance(
                asset=Asset(chain="BASE", chain_id=8453, symbol="ETH", decimals=18),
                amount=Decimal("2"),
                amount_raw="2000000000000000000",
            )
        ]


def wallet_context():
    return {"address": WALLET, "chain": "BASE", "chain_id": 8453, "native_symbol": "ETH"}


class TransactionExplorer:
    async def get_transaction_detail(self, chain, tx_hash):
        from wallet_agent.domain.models import TransactionDetail, TransactionStatus

        return TransactionDetail(
            chain=chain,
            chain_id="1",
            tx_hash=tx_hash,
            status=TransactionStatus.CONFIRMED,
            source="okx",
            history_kind="full",
        )


def turn(message):
    return {
        "conversation_id": "task-memory",
        "user_id": "eval-user",
        "request": {"message": message},
        "wallet_context": wallet_context(),
    }


@pytest.mark.asyncio
async def test_transfer_clarification_retains_known_slots_in_active_task():
    model = UnderstandingModel(
        ["transfer"],
        transfer=[TransferSlotPatch(chain="BASE", symbol="ETH", amount="0.01")],
    )
    graph = build_graph(model=model, chains={"BASE": Chain()})

    result = await graph.ainvoke(
        turn("从当前 Base 钱包转 0.01 ETH"),
        config={"configurable": {"thread_id": "task-transfer"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert result["response"]["missing_fields"] == ["transfer_recipient"]
    assert result["active_task"]["kind"] == "transfer"
    assert result["active_task"]["slots"] == {
        "chain": "BASE",
        "symbol": "ETH",
        "amount": "0.01",
    }
    assert result["transfer_draft"]["transfer_amount"] == "0.01"


@pytest.mark.asyncio
async def test_transfer_suggestions_match_missing_recipient_and_use_wallet_address():
    model = UnderstandingModel(
        ["transfer"],
        transfer=[TransferSlotPatch(chain="BASE", symbol="ETH", amount="0.01")],
    )
    graph = build_graph(model=model, chains={"BASE": Chain()})

    result = await graph.ainvoke(
        turn("从当前 Base 钱包转 0.01 ETH"),
        config={"configurable": {"thread_id": "transfer-suggestions"}},
    )

    suggestion = result["response"]["suggestions"][0]
    assert suggestion["label"] == "使用当前钱包地址"
    assert suggestion["data"]["recipient"] == WALLET
    assert "transfer_recipient" in result["response"]["missing_fields"]


@pytest.mark.asyncio
async def test_transfer_followup_current_wallet_address_continues_active_task():
    model = UnderstandingModel(
        ["transfer"],
        transfer=[
            TransferSlotPatch(chain="BASE", symbol="ETH", amount="0.01"),
            TransferSlotPatch(),
        ],
    )
    graph = build_graph(model=model, chains={"BASE": Chain()})
    config = {"configurable": {"thread_id": "transfer-wallet-followup"}}

    first = await graph.ainvoke(turn("转 0.01 ETH"), config=config)
    assert first["response"]["missing_fields"] == ["transfer_recipient"]

    second = await graph.ainvoke(turn("使用当前钱包地址"), config=config)
    assert second["response"]["kind"] == "transfer_prepare"
    assert second["transfer_request"]["recipient"] == WALLET


@pytest.mark.asyncio
async def test_transfer_followup_bsc_usdc_resolves_wallet_token_metadata():
    wallet_usdc = "0x" + "8" * 40
    model = UnderstandingModel(
        ["transfer"],
        transfer=[
            TransferSlotPatch(amount="1", recipient=RECIPIENT),
            TransferSlotPatch(chain="BSC", symbol="USDC"),
        ],
    )
    graph = build_graph(model=model, chains={"BSC": BscTokenChain()})
    config = {"configurable": {"thread_id": "transfer-bsc-usdc-followup"}}
    context = {
        "address": WALLET,
        "chain": "BSC",
        "chain_id": 56,
        "token_balances": [
            {
                "asset": {
                    "chain": "BSC",
                    "chain_id": 56,
                    "symbol": "USDC",
                    "decimals": 18,
                    "address": wallet_usdc,
                }
            }
        ],
    }

    first = await graph.ainvoke(
        {**turn("转 1 到这个地址"), "wallet_context": {"address": WALLET}}, config=config
    )
    assert first["response"]["missing_fields"] == ["transfer_chain"]

    second = await graph.ainvoke(
        {**turn("BSC 上的 USDC"), "wallet_context": context}, config=config
    )
    assert second["response"]["kind"] == "transfer_prepare"
    assert second["transfer_request"]["token"]["address"] == wallet_usdc
    assert second["transfer_request"]["token"]["decimals"] == 18
    assert second["active_task"]["slots"]["token_address"] == wallet_usdc


@pytest.mark.asyncio
async def test_transfer_uses_wallet_provider_for_token_metadata_when_context_has_no_balances():
    model = UnderstandingModel(
        ["transfer"],
        transfer=[
            TransferSlotPatch(
                chain="BSC",
                symbol="USDC",
                amount="1",
                recipient=RECIPIENT,
            )
        ],
    )
    graph = build_graph(
        model=model,
        chains={"BSC": BscTokenChain()},
        wallet_provider=BscWalletProvider(),
    )

    result = await graph.ainvoke(
        {
            **turn(f"给 {RECIPIENT} 转 1 USDC"),
            "wallet_context": {"address": WALLET, "chain": "BSC", "chain_id": 56},
        },
        config={"configurable": {"thread_id": "transfer-wallet-provider-metadata"}},
    )

    assert result["response"]["kind"] == "transfer_prepare"
    assert result["transfer_request"]["token"]["address"] == (
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
    )
    assert result["transfer_request"]["token"]["decimals"] == 18


@pytest.mark.asyncio
async def test_new_transfer_resets_completed_swap_lifecycle_state():
    old_order = ProviderOrder(
        provider="bridgers",
        provider_order_id="old-order",
        provider_reference="old-reference",
        tx_hash="0x" + "a" * 64,
    )
    model = UnderstandingModel(
        ["transfer"],
        transfer=[
            TransferSlotPatch(
                chain="BASE",
                symbol="ETH",
                amount="0.01",
                recipient=RECIPIENT,
            )
        ],
    )
    graph = build_graph(model=model, chains={"BASE": Chain()})
    result = await graph.ainvoke(
        {
            **turn(f"转 0.01 ETH 给 {RECIPIENT}"),
            "active_task": {
                "task_id": "old-swap",
                "kind": "swap",
                "status": "completed",
                "stage": "completed",
                "revision": 2,
                "slots": {},
                "slot_sources": {},
            },
            "provider_orders": {"bridgers": old_order.model_dump(mode="json")},
            "broadcast_tx_hash": old_order.tx_hash,
            "broadcast_status": "confirmed",
            "selected_quote": {"provider_reference": "old-reference"},
        },
        config={"configurable": {"thread_id": "transfer-after-swap"}},
    )

    assert result["response"]["kind"] == "transfer_prepare"
    assert result["operation_reset"] is True
    assert result["provider_orders"] == {}
    assert result.get("selected_quote") is None
    assert result.get("broadcast_tx_hash") is None


@pytest.mark.asyncio
async def test_transaction_status_followups_merge_hash_then_chain_across_turns():
    tx_hash = "0x" + "d" * 64
    graph = build_graph(
        model=TransactionStatusModel(
            [
                {},
                {},
                {},
            ]
        ),
        explorer_provider=TransactionExplorer(),
    )
    config = {"configurable": {"thread_id": "transaction-status-followups"}}

    first = await graph.ainvoke(turn("到账了吗"), config=config)
    assert first["response"]["kind"] == "clarification"
    assert first["response"]["missing_fields"] == [
        "transaction_chain",
        "transaction_hash",
    ]

    second = await graph.ainvoke(turn(tx_hash), config=config)
    assert second["response"]["kind"] == "clarification"
    assert second["response"]["missing_fields"] == ["transaction_chain"]
    assert second["transaction_query"]["tx_hash"] == tx_hash

    third = await graph.ainvoke(turn("以太"), config=config)
    assert third["response"]["kind"] == "transaction_status"
    assert third["response"]["status"] == "confirmed"
    assert third["transaction_query"] == {"chain": "ETH", "tx_hash": tx_hash}


@pytest.mark.asyncio
async def test_transaction_status_uses_registered_transfer_broadcast():
    tx_hash = "0x" + "e" * 64
    graph = build_graph(
        model=TransactionStatusModel([{}]),
        explorer_provider=TransactionExplorer(),
    )

    result = await graph.ainvoke(
        {
            **turn("到账了吗"),
            "broadcast_tx_hash": tx_hash,
            "pending_transaction": {
                "chain": "ETH",
                "chain_id": 1,
                "to": RECIPIENT,
                "data": "0x",
                "value": "1",
            },
        },
        config={"configurable": {"thread_id": "registered-transfer-status"}},
    )

    assert result["response"]["kind"] == "transaction_status"
    assert result["response"]["status"] == "confirmed"
    assert result["transaction_query"] == {"chain": "ETH", "tx_hash": tx_hash}


@pytest.mark.asyncio
async def test_transaction_status_infers_chain_from_selected_swap_quote():
    tx_hash = "0x" + "9" * 64
    source = Asset(chain="ETH", chain_id=1, symbol="USDC", decimals=6, address=USDC)
    destination = Asset(chain="BSC", chain_id=56, symbol="USDT", decimals=6, address=USDT)
    quote = NormalizedQuote(
        provider="bridgers",
        source_asset=source,
        destination_asset=destination,
        input_amount=Decimal("1"),
        input_amount_raw="1000000",
        expected_output=Decimal("1"),
        expected_output_raw="1000000",
        provider_reference="selected-chain",
    )
    graph = build_graph(
        model=TransactionStatusModel([{}]),
        explorer_provider=TransactionExplorer(),
    )

    result = await graph.ainvoke(
        {
            **turn("源链交易确认了吗？"),
            "broadcast_tx_hash": tx_hash,
            "selected_quote": quote.model_dump(mode="json"),
            "pending_transaction": None,
        },
        config={"configurable": {"thread_id": "selected-quote-transaction-chain"}},
    )

    assert result["response"]["kind"] == "transaction_status"
    assert result["transaction_query"] == {"chain": "ETH", "tx_hash": tx_hash}


@pytest.mark.asyncio
async def test_transaction_status_uses_local_not_propagated_broadcast_before_explorer():
    tx_hash = "0x" + "f" * 64

    class BrokenExplorer:
        async def get_transaction_detail(self, _chain, _tx_hash):
            raise RuntimeError("OKX transaction hash is missing")

    graph = build_graph(
        model=TransactionStatusModel([{}]),
        explorer_provider=BrokenExplorer(),
    )

    result = await graph.ainvoke(
        {
            **turn("转完了吗"),
            "broadcast_tx_hash": tx_hash,
            "broadcast_status": "not_propagated",
            "pending_transaction": {
                "chain": "ETH",
                "chain_id": 1,
                "to": RECIPIENT,
                "data": "0x",
                "value": "1",
            },
        },
        config={"configurable": {"thread_id": "local-broadcast-status"}},
    )

    assert result["response"] == {
        "kind": "transaction_status",
        "chain": "ETH",
        "tx_hash": tx_hash,
        "status": "pending",
        "broadcast_status": "not_propagated",
        "source": "local",
        "message": "交易哈希暂时还没有在源链上出现，请稍后重试。",
    }


@pytest.mark.asyncio
async def test_transaction_status_reports_prepared_transfer_has_not_been_broadcast():
    class UnexpectedExplorer:
        async def get_transaction_detail(self, _chain, _tx_hash):
            raise AssertionError("prepared transfers must not call the explorer")

    graph = build_graph(
        model=TransactionStatusModel([{}]),
        explorer_provider=UnexpectedExplorer(),
    )

    result = await graph.ainvoke(
        {
            **turn("转完了吗"),
            "pending_transaction": {
                "chain": "ETH",
                "chain_id": 1,
                "to": RECIPIENT,
                "data": "0x",
                "value": "1",
            },
        },
        config={"configurable": {"thread_id": "prepared-transfer-status"}},
    )

    assert result["response"] == {
        "kind": "transaction_status",
        "chain": "ETH",
        "status": "not_broadcast",
        "source": "local",
        "message": "交易尚未广播，请先在钱包中签名并广播。",
    }


@pytest.mark.asyncio
async def test_registered_transfer_replaces_stale_transaction_query():
    old_hash = "0x" + "a" * 64
    registered_hash = "0x" + "b" * 64
    graph = build_graph(
        model=TransactionStatusModel([{}]),
        explorer_provider=TransactionExplorer(),
    )

    result = await graph.ainvoke(
        {
            **turn("到账了吗"),
            "transaction_query": {"chain": "BASE", "tx_hash": old_hash},
            "broadcast_tx_hash": registered_hash,
            "pending_transaction": {
                "chain": "ETH",
                "chain_id": 1,
                "to": RECIPIENT,
                "data": "0x",
                "value": "1",
            },
        },
        config={"configurable": {"thread_id": "registered-transfer-replaces-stale"}},
    )

    assert result["transaction_query"] == {
        "chain": "ETH",
        "tx_hash": registered_hash,
    }


@pytest.mark.asyncio
async def test_new_transfer_clears_previous_transaction_lifecycle_state():
    old_hash = "0x" + "a" * 64
    model = UnderstandingModel(
        ["transfer"],
        transfer=[
            TransferSlotPatch(
                chain="BASE",
                symbol="ETH",
                amount="0.0001",
                recipient=RECIPIENT,
            )
        ],
    )
    graph = build_graph(model=model, chains={"BASE": Chain()})

    result = await graph.ainvoke(
        {
            **turn(f"转 0.0001 ETH 给 {RECIPIENT}"),
            "broadcast_tx_hash": old_hash,
            "broadcast_status": "not_propagated",
            "transaction_query": {"chain": "ETH", "tx_hash": old_hash},
            "transaction_status_snapshot": {
                "chain": "ETH",
                "tx_hash": old_hash,
                "status": "pending",
            },
            "pending_transaction": {
                "chain": "ETH",
                "chain_id": 1,
                "to": RECIPIENT,
                "data": "0x",
                "value": "1",
            },
        },
        config={"configurable": {"thread_id": "new-transfer-clears-old-status"}},
    )

    assert result["response"]["kind"] == "transfer_prepare"
    assert result["pending_transaction"]["chain"] == "BASE"
    assert result.get("broadcast_tx_hash") is None
    assert result.get("broadcast_status") is None
    assert result.get("transaction_query") is None
    assert result.get("transaction_status_snapshot") is None


@pytest.mark.asyncio
async def test_explicit_transaction_hints_survive_extractor_failure():
    tx_hash = "0x" + "c" * 64
    graph = build_graph(
        model=TransactionStatusModel([RuntimeError("model unavailable")]),
        explorer_provider=TransactionExplorer(),
    )

    result = await graph.ainvoke(
        turn(f"以太，{tx_hash}"),
        config={"configurable": {"thread_id": "transaction-hints-model-failure"}},
    )

    assert result["response"]["kind"] == "transaction_status"
    assert result["transaction_query"] == {"chain": "ETH", "tx_hash": tx_hash}


@pytest.mark.asyncio
async def test_transfer_asset_change_without_explicit_amount_does_not_reuse_previous_amount():
    model = UnderstandingModel(
        ["transfer", "transfer"],
        transfer=[
            TransferSlotPatch(
                chain="BASE",
                symbol="USDC",
                amount="1",
                recipient=RECIPIENT,
            ),
            TransferSlotPatch(chain="BASE", symbol="ETH", recipient=RECIPIENT),
        ],
    )
    graph = build_graph(model=model, providers=[Provider()], chains={"BASE": Chain()})
    config = {"configurable": {"thread_id": "task-transfer-asset-without-amount"}}

    first = await graph.ainvoke(turn(f"转 1 USDC 给 {RECIPIENT}"), config=config)
    assert first["active_task"]["slots"]["amount"] == "1"

    second = await graph.ainvoke(turn("转一些 ETH 到这个地址"), config=config)

    assert second["response"]["kind"] == "clarification"
    assert "transfer_amount" in second["response"]["missing_fields"]
    assert "amount" not in second["active_task"]["slots"]
    assert "amount_raw" not in second["active_task"]["slots"]
    assert second.get("pending_transaction") is None


@pytest.mark.asyncio
async def test_transfer_native_followup_does_not_inherit_previous_bsc_token_metadata():
    model = UnderstandingModel(
        ["transfer", "transfer", "clarification"],
        transfer=[
            TransferSlotPatch(
                chain="BSC",
                symbol="USDC",
                token_address="0x" + "8" * 40,
                decimals=18,
                amount="1",
                recipient=WALLET,
            ),
            TransferSlotPatch(symbol="ETH", recipient=WALLET),
            TransferSlotPatch(amount="0.00001"),
        ],
    )
    graph = build_graph(model=model, chains={"BSC": BscTokenChain(), "ETH": Chain()})
    config = {"configurable": {"thread_id": "transfer-native-after-bsc-token"}}
    context = {"address": WALLET, "chain": "ETH", "chain_id": 1, "native_symbol": "ETH"}

    first = await graph.ainvoke(
        {**turn("转 BSC 上的 USDC"), "wallet_context": context}, config=config
    )
    assert first["response"]["kind"] == "transfer_prepare"

    second = await graph.ainvoke(
        {**turn("给自己转一点 ETH"), "wallet_context": context}, config=config
    )
    assert second["response"]["missing_fields"] == ["transfer_amount"]
    assert second["active_task"]["slots"] == {
        "chain": "ETH",
        "symbol": "ETH",
        "recipient": WALLET,
    }

    third = await graph.ainvoke(
        {**turn("0.00001"), "wallet_context": context}, config=config
    )
    assert third["response"]["kind"] == "transfer_prepare"
    assert third["transfer_request"]["chain"] == "ETH"
    assert third["transfer_request"]["token"] is None


@pytest.mark.asyncio
async def test_transfer_native_resume_cleans_stale_persisted_token_metadata():
    model = UnderstandingModel(
        ["clarification"],
        transfer=[TransferSlotPatch()],
    )
    graph = build_graph(model=model, chains={"ETH": Chain()})
    stale_task = {
        "task_id": "stale-native-transfer",
        "kind": "transfer",
        "status": "collecting",
        "stage": "collecting_parameters",
        "revision": 4,
        "slots": {
            "chain": "ETH",
            "symbol": "ETH",
            "token_address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "transfer_chain_id": 1,
            "recipient": WALLET,
        },
        "slot_sources": {},
        "missing_fields": ["transfer_amount"],
    }

    result = await graph.ainvoke(
        {
            **turn("0.0001"),
            "active_task": stale_task,
        },
        config={"configurable": {"thread_id": "stale-native-transfer"}},
    )

    assert result["response"]["kind"] == "transfer_prepare"
    assert result["transfer_request"]["token"] is None
    assert result["active_task"]["slots"]["symbol"] == "ETH"
    assert "token_address" not in result["active_task"]["slots"]


@pytest.mark.asyncio
async def test_swap_follow_up_classified_as_clarification_still_merges_chain_patch():
    model = UnderstandingModel(
        ["swap_quote", "clarification"],
        swap=[
            SwapSlotPatch(source_symbol="USDC", destination_symbol="USDT", input_amount="1"),
            SwapSlotPatch(source_chain="BASE", destination_chain="BASE"),
        ],
    )
    provider = Provider()
    graph = build_graph(model=model, providers=[provider])
    config = {"configurable": {"thread_id": "task-swap-followup"}}

    first = await graph.ainvoke(turn("用 1 USDC 换 USDT"), config=config)
    second = await graph.ainvoke(turn("都在 Base 链"), config=config)

    assert first["response"]["kind"] == "clarification"
    assert second["response"]["kind"] == "swap_quote"
    assert second["active_task"]["kind"] == "swap"
    assert second["active_task"]["revision"] == 2
    assert second["active_task"]["slots"]["source_symbol"] == "USDC"
    assert provider.quote_calls == 1
    assert model.extracted_kinds == ["swap", "swap"]


@pytest.mark.asyncio
async def test_wallet_connected_followup_rebuilds_active_swap_request():
    model = UnderstandingModel(
        ["swap_prepare"],
        swap=[SwapSlotPatch()],
    )
    provider = Provider()
    graph = build_graph(model=model, providers=[provider])

    result = await graph.ainvoke(
        {
            **turn("已经连接"),
            "active_task": {
                "task_id": "wallet-followup-swap",
                "kind": "swap",
                "status": "collecting",
                "stage": "collecting_parameters",
                "revision": 1,
                "slots": {
                    "source_chain": "BASE",
                    "destination_chain": "BASE",
                    "source_symbol": "USDC",
                    "destination_symbol": "USDT",
                    "source_token_address": USDC,
                    "destination_token_address": USDT,
                    "source_decimals": 6,
                    "destination_decimals": 6,
                    "input_amount": "1",
                },
                "slot_sources": {},
                "missing_fields": ["sender_address", "recipient_address"],
            },
        },
        config={"configurable": {"thread_id": "wallet-connected-swap-followup"}},
    )

    assert result["response"]["kind"] == "swap_quote"
    assert result["swap_request"]["sender_address"] == WALLET
    assert result["swap_request"]["recipient_address"] == WALLET
    assert provider.quote_calls == 1
    assert "MISSING_SWAP_PARAMETERS" not in {
        error["code"] for error in result.get("errors", [])
    }


@pytest.mark.asyncio
async def test_active_swap_followup_skips_reclassification_and_uses_deterministic_reply():
    class NoResponseModel(UnderstandingModel):
        async def respond(self, _request, _facts):
            raise AssertionError("parameter collection must not call the response model")

    model = NoResponseModel(
        ["swap_quote"],
        swap=[
            SwapSlotPatch(destination_symbol="ETH"),
            SwapSlotPatch(source_symbol="BNB", destination_symbol="ETH"),
        ],
    )
    graph = build_graph(model=model)
    config = {"configurable": {"thread_id": "fast-active-swap-followup"}}

    await graph.ainvoke(turn("我想兑换 ETH"), config=config)
    result = await graph.ainvoke(
        turn("用 BNB 兑换成以太坊 ETH"), config=config
    )

    assert result["supervisor_decision"] == {
        "intent": "swap_quote",
        "source": "active_task",
    }
    assert result["active_task"]["slots"]["source_chain"] == "BSC"
    assert result["active_task"]["slots"]["destination_chain"] == "ETH"
    assert result["response"]["missing_fields"] == ["input_amount"]
    assert "用 BNB 兑换 ETH" in result["response"]["message"]
    assert "准备投入多少 BNB" in result["response"]["message"]


@pytest.mark.asyncio
async def test_usd_value_target_converts_to_exact_output_and_reverse_quotes_source_amount():
    model = UnderstandingModel(
        ["swap_quote"],
        swap=[
            SwapSlotPatch(
                source_chain="BSC",
                destination_chain="ETH",
                source_symbol="BNB",
                destination_symbol="ETH",
            )
        ],
    )
    provider = Provider()
    graph = build_graph(model=model, providers=[provider], price_provider=FixedPrices())

    result = await graph.ainvoke(
        turn("用 BNB 兑换价值 10u 的以太坊 ETH"),
        config={"configurable": {"thread_id": "usd-value-exact-output"}},
    )

    assert result["response"]["kind"] == "swap_quote"
    assert Decimal(result["swap_draft"]["output_amount"]) == Decimal("0.005")
    assert result["swap_draft"]["target_value_amount"] == "10"
    assert result["swap_draft"]["target_value_currency"] == "USD"
    assert result["swap_request"]["amount_mode"] == "exact_out"
    assert Decimal(result["response"]["quotes"][0]["input_amount"]) == Decimal("0.01")
    assert Decimal(result["response"]["quotes"][0]["expected_output"]) == Decimal("0.005")
    assert provider.quote_calls == 1


@pytest.mark.asyncio
async def test_swap_corrects_wrong_model_chain_for_unique_native_source():
    class NativeOnlyProvider(Provider):
        async def list_assets(self, _query):
            raise AssertionError("native BNB and ETH must not use token discovery")

    model = UnderstandingModel(
        ["swap_quote"],
        swap=[
            SwapSlotPatch(
                source_chain="ETH",
                destination_chain="ETH",
                source_symbol="BNB",
                destination_symbol="ETH",
            )
        ],
    )
    provider = NativeOnlyProvider()
    graph = build_graph(model=model, providers=[provider], price_provider=FixedPrices())

    result = await graph.ainvoke(
        turn("用bnb 兑换价值 10u 的 以太坊 ETH"),
        config={"configurable": {"thread_id": "wrong-model-native-source-chain"}},
    )

    assert result["response"]["kind"] == "swap_quote"
    assert result["active_task"]["slots"]["source_chain"] == "BSC"
    assert result["swap_request"]["source_asset"]["chain"] == "BSC"
    assert result["swap_request"]["source_asset"]["address"] is None
    assert result["swap_request"]["destination_asset"]["chain"] == "ETH"
    assert provider.quote_calls == 1


@pytest.mark.asyncio
async def test_legacy_active_swap_recovers_usd_target_from_history_on_calculation_followup():
    model = UnderstandingModel(["swap_quote"], swap=[SwapSlotPatch()])
    provider = Provider()
    graph = build_graph(model=model, providers=[provider], price_provider=FixedPrices())
    active_task = {
        "task_id": "legacy-usd-value",
        "kind": "swap",
        "status": "collecting",
        "stage": "collecting_parameters",
        "revision": 1,
        "slots": {
            "source_chain": "BSC",
            "destination_chain": "ETH",
            "source_symbol": "BNB",
            "destination_symbol": "ETH",
            "source_decimals": 18,
            "destination_decimals": 18,
        },
        "slot_sources": {},
        "missing_fields": ["input_amount"],
        "updated_by": "system",
    }

    result = await graph.ainvoke(
        {
            **turn("你能计算一下吗"),
            "active_task": active_task,
            "swap_draft": active_task["slots"],
            "conversation_history": [
                {"role": "user", "content": "用 BNB 兑换价值 10u 的以太坊 ETH"},
                {"role": "assistant", "content": "请提供 BNB 数量"},
                {"role": "user", "content": "你能计算一下吗"},
            ],
        },
        config={"configurable": {"thread_id": "legacy-usd-value-recovery"}},
    )

    assert result["response"]["kind"] == "swap_quote"
    assert result["swap_draft"]["target_value_amount"] == "10"
    assert Decimal(result["response"]["quotes"][0]["input_amount"]) == Decimal("0.01")


@pytest.mark.asyncio
async def test_usd_value_target_reports_missing_price_provider_without_guessing():
    model = UnderstandingModel(
        ["swap_quote"],
        swap=[
            SwapSlotPatch(
                source_chain="BSC",
                destination_chain="ETH",
                source_symbol="BNB",
                destination_symbol="ETH",
            )
        ],
    )
    graph = build_graph(model=model, providers=[Provider()])

    result = await graph.ainvoke(
        turn("用 BNB 兑换价值 10u 的以太坊 ETH"),
        config={"configurable": {"thread_id": "usd-value-no-price"}},
    )

    assert result["response"]["kind"] == "error"
    assert result["response"]["errors"][0]["code"] == "PRICE_PROVIDER_UNAVAILABLE"
    assert result.get("quote_candidates") == []


@pytest.mark.asyncio
async def test_ethereum_swap_resolves_provider_metadata_then_quotes_amount_with_unit():
    model = UnderstandingModel(
        ["swap_quote", "clarification", "clarification", "clarification"],
        swap=[
            SwapSlotPatch(source_symbol="USDT"),
            SwapSlotPatch(destination_chain="以太坊"),
            SwapSlotPatch(source_chain="以太坊", source_symbol="USDC"),
            SwapSlotPatch(),
        ],
    )
    provider = EthereumCatalogProvider()
    graph = build_graph(model=model, providers=[provider])
    config = {"configurable": {"thread_id": "task-ethereum-catalog"}}

    first = await graph.ainvoke(
        {
            "conversation_id": "task-ethereum-catalog",
            "user_id": "eval-user",
            "request": {"message": "换一些 USDT"},
        },
        config=config,
    )
    assert first["active_task"]["slots"] == {"destination_symbol": "USDT"}
    assert "destination_symbol" not in first["response"]["missing_fields"]
    assert first["response"]["suggestions"][0]["message"] == "用当前网络上的 USDC 换"
    assert first["response"]["suggestions"][0]["data"]["destination_symbol"] == "USDT"
    await graph.ainvoke(
        {
            "conversation_id": "task-ethereum-catalog",
            "user_id": "eval-user",
            "request": {"message": "换一些以太上的 USDT"},
        },
        config=config,
    )
    third = await graph.ainvoke(
        {
            "conversation_id": "task-ethereum-catalog",
            "user_id": "eval-user",
            "request": {"message": "用以太上的 USDC"},
            "wallet_context": {
                "address": WALLET,
                "chain": "ETH",
                "chain_id": 1,
                "native_symbol": "ETH",
            },
        },
        config=config,
    )

    assert third["response"]["kind"] == "clarification"
    assert third["response"]["missing_fields"] == ["input_amount"]
    assert third["response"]["suggestions"] == [
        {
            "label": "填写 USDC 数量",
            "message": "请输入 USDC 数量",
            "data": {
                "intent": "swap_quote",
                "destination_symbol": "USDT",
                "destination_chain": "ETH",
                "destination_token_address": USDT,
                "source_chain": "ETH",
                "source_symbol": "USDC",
                "source_token_address": USDC,
            },
        }
    ]
    assert third["active_task"]["revision"] == 3
    assert third["active_task"]["slots"] == {
        "destination_symbol": "USDT",
        "source_chain": "ETH",
        "destination_chain": "ETH",
        "source_symbol": "USDC",
        "source_token_address": USDC,
        "source_decimals": 6,
        "source_chain_id": 1,
        "source_name": "USD Coin",
        "source_logo_url": "https://example.invalid/usdc.png",
        "destination_token_address": USDT,
        "destination_decimals": 6,
        "destination_chain_id": 1,
        "destination_name": "Tether",
        "destination_logo_url": "https://example.invalid/usdt.png",
    }
    assert third["active_task"]["slot_sources"]["source_token_address"] == "resolver"
    assert third["active_task"]["slot_sources"]["destination_token_address"] == "resolver"

    fourth = await graph.ainvoke(
        {
            "conversation_id": "task-ethereum-catalog",
            "user_id": "eval-user",
            "request": {"message": "1 USDC"},
            "wallet_context": {
                "address": WALLET,
                "chain": "ETH",
                "chain_id": 1,
                "native_symbol": "ETH",
            },
        },
        config=config,
    )

    assert fourth["response"]["kind"] == "swap_quote"
    assert fourth["active_task"]["revision"] == 4
    assert fourth["active_task"]["slots"]["input_amount"] == "1"
    assert provider.quote_calls == 1


@pytest.mark.asyncio
async def test_swap_suggests_known_source_chain_instead_of_different_wallet_chain():
    model = UnderstandingModel(
        ["swap_quote"],
        swap=[
            SwapSlotPatch(
                source_chain="ETH",
                source_symbol="USDC",
                destination_symbol="USDT",
            )
        ],
    )
    graph = build_graph(model=model, providers=[EthereumCatalogProvider()])

    result = await graph.ainvoke(
        {
            "conversation_id": "cross-chain-suggestion",
            "user_id": "eval-user",
            "request": {"message": "用 ETH 上的 USDC 换 USDT"},
            "wallet_context": {"address": WALLET, "chain": "BASE", "chain_id": 8453},
        },
        config={"configurable": {"thread_id": "cross-chain-suggestion"}},
    )

    assert result["response"]["missing_fields"] == ["destination_chain", "input_amount"]
    assert result["response"]["suggestions"][0] == {
        "label": "目标也在 ETH 网络",
        "message": "目标也在 ETH 链",
        "data": {
            "intent": "swap_quote",
            "source_chain": "ETH",
            "source_symbol": "USDC",
            "destination_symbol": "USDT",
            "source_token_address": USDC,
        },
    }


@pytest.mark.asyncio
async def test_swap_infers_unique_native_destination_chain_after_source_chain_follow_up():
    model = UnderstandingModel(
        ["swap_quote", "clarification"],
        swap=[
            SwapSlotPatch(
                source_symbol="USDT",
                destination_symbol="BNB",
                input_amount="10",
            ),
            SwapSlotPatch(source_chain="ETH"),
        ],
    )
    provider = EthereumCatalogProvider()
    graph = build_graph(model=model, providers=[provider])
    config = {"configurable": {"thread_id": "unique-native-destination-chain"}}

    first = await graph.ainvoke(turn("把 10usdt 换成bnb"), config=config)
    second = await graph.ainvoke(turn("从以太换上换"), config=config)

    assert first["response"]["kind"] == "clarification"
    assert first["active_task"]["slots"]["destination_chain"] == "BSC"
    assert second["response"]["kind"] == "swap_quote"
    assert second["active_task"]["revision"] == 2
    assert second["active_task"]["slots"]["destination_chain"] == "BSC"
    assert second["swap_request"]["destination_asset"] == {
        "chain": "BSC",
        "chain_id": 56,
        "symbol": "BNB",
        "address": None,
        "decimals": 18,
        "name": None,
        "logo_url": None,
    }
    assert provider.quote_calls == 1


@pytest.mark.asyncio
async def test_swap_corrects_extractor_chain_leak_for_unique_native_destination():
    model = UnderstandingModel(
        ["swap_quote"],
        swap=[
            SwapSlotPatch(
                source_chain="ETH",
                source_symbol="USDC",
                destination_chain="ETH",
                destination_symbol="BNB",
                input_amount="10",
            )
        ],
    )
    provider = EthereumCatalogProvider()
    graph = build_graph(model=model, providers=[provider])

    result = await graph.ainvoke(
        turn("以太上 10 USDC 换 BNB"),
        config={"configurable": {"thread_id": "source-chain-does-not-leak-to-native-target"}},
    )

    assert result["response"]["kind"] == "swap_quote"
    assert result["active_task"]["slots"]["source_chain"] == "ETH"
    assert result["active_task"]["slots"]["destination_chain"] == "BSC"
    assert result["swap_request"]["destination_asset"]["chain"] == "BSC"
    assert provider.quote_calls == 1


@pytest.mark.asyncio
async def test_swap_does_not_infer_ambiguous_eth_destination_chain():
    model = UnderstandingModel(
        ["swap_quote"],
        swap=[
            SwapSlotPatch(
                source_chain="BASE",
                source_symbol="USDC",
                destination_symbol="ETH",
                input_amount="1",
            )
        ],
    )
    provider = Provider()
    graph = build_graph(model=model, providers=[provider])

    result = await graph.ainvoke(
        turn("在 Base 用 1 USDC 换 ETH"),
        config={"configurable": {"thread_id": "ambiguous-native-destination-chain"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert "destination_chain" in result["response"]["missing_fields"]
    assert "destination_chain" not in result["active_task"]["slots"]
    assert provider.quote_calls == 0


@pytest.mark.asyncio
async def test_swap_explicit_token_address_prevents_native_chain_inference():
    explicit_bnb_token = "0x" + "b" * 40
    model = UnderstandingModel(
        ["swap_quote"],
        swap=[
            SwapSlotPatch(
                source_chain="ETH",
                source_symbol="USDT",
                destination_symbol="BNB",
                destination_token_address=explicit_bnb_token,
                input_amount="10",
            )
        ],
    )
    provider = EthereumCatalogProvider()
    graph = build_graph(model=model, providers=[provider])

    result = await graph.ainvoke(
        turn("用 ETH 上的 10 USDT 换合约 BNB"),
        config={"configurable": {"thread_id": "explicit-token-is-not-native"}},
    )

    assert result["response"]["kind"] == "clarification"
    assert "destination_chain" in result["response"]["missing_fields"]
    assert result["active_task"]["slots"]["destination_token_address"] == explicit_bnb_token
    assert "destination_chain" not in result["active_task"]["slots"]
    assert provider.quote_calls == 0


@pytest.mark.asyncio
async def test_transient_balance_query_preserves_active_swap_task():
    model = UnderstandingModel(
        ["swap_quote", "wallet_query"],
        swap=[SwapSlotPatch(source_symbol="USDC", destination_symbol="USDT", input_amount="1")],
    )
    graph = build_graph(
        model=model,
        providers=[Provider()],
        chains={"BASE": Chain()},
        wallet_provider=OkxWalletProvider(),
    )
    config = {"configurable": {"thread_id": "task-transient-balance"}}

    first = await graph.ainvoke(turn("用 1 USDC 换 USDT"), config=config)
    second = await graph.ainvoke(turn("先看看我的余额"), config=config)

    assert second["response"]["kind"] == "wallet_query"
    assert second["active_task"] == first["active_task"]
    assert second["active_task"]["status"] == "collecting"


@pytest.mark.asyncio
async def test_swap_correction_replaces_previous_quote():
    model = UnderstandingModel(
        ["swap_quote", "swap_quote"],
        swap=[
            SwapSlotPatch(
                source_chain="BASE",
                destination_chain="BASE",
                source_symbol="USDC",
                destination_symbol="USDT",
                input_amount="1",
            ),
            SwapSlotPatch(input_amount="2"),
        ],
    )
    provider = Provider()
    graph = build_graph(model=model, providers=[provider])
    config = {"configurable": {"thread_id": "task-swap-correction"}}

    first = await graph.ainvoke(turn("在 Base 用 1 USDC 换 USDT"), config=config)
    second = await graph.ainvoke(turn("改成 2 USDC"), config=config)

    assert first["response"]["quotes"][0]["provider_reference"] == "quote-1"
    assert [item["provider_reference"] for item in second["response"]["quotes"]] == ["quote-2"]
    assert second["active_task"]["revision"] == 2
    assert second["swap_request"]["input_amount"] == "2"


@pytest.mark.asyncio
async def test_swap_accepts_user_slippage_and_passes_it_to_quote():
    model = UnderstandingModel(
        ["swap_quote"],
        swap=[
            SwapSlotPatch(
                source_chain="BASE",
                destination_chain="BASE",
                source_symbol="USDC",
                destination_symbol="USDT",
                input_amount="1",
                slippage_bps=250,
            )
        ],
    )
    provider = Provider()
    graph = build_graph(model=model, providers=[provider])

    result = await graph.ainvoke(
        turn("在 Base 用 1 USDC 换 USDT，滑点 2.5%"),
        config={"configurable": {"thread_id": "task-swap-slippage"}},
    )

    assert result["swap_request"]["slippage_bps"] == 250
    assert result["active_task"]["slots"]["slippage_bps"] == 250
    assert provider.last_slippage_bps == 250


@pytest.mark.asyncio
async def test_target_token_amount_is_preserved_as_exact_output():
    model = UnderstandingModel(
        ["swap_quote", "clarification"],
        swap=[
            SwapSlotPatch(
                source_chain="BASE",
                destination_chain="BASE",
                source_symbol="USDC",
                destination_symbol="USDT",
            ),
            SwapSlotPatch(output_amount="5", amount_mode="exact_out"),
        ],
    )
    provider = Provider()
    graph = build_graph(model=model, providers=[provider])
    config = {"configurable": {"thread_id": "task-target-amount"}}

    first = await graph.ainvoke(turn("在 Base 用 USDC 换 USDT"), config=config)
    assert first["response"]["kind"] == "clarification"
    second = await graph.ainvoke(turn("我想换 5USDT"), config=config)

    assert second["response"]["kind"] == "swap_quote"
    assert second["active_task"]["slots"]["output_amount"] == "5"
    assert second["active_task"]["slots"]["amount_mode"] == "exact_out"
    assert "input_amount" not in second["active_task"]["slots"]
    assert second["swap_request"]["amount_mode"] == "exact_out"
    assert second["response"]["quotes"][0]["expected_output"] == "5"
    assert provider.quote_calls == 1


@pytest.mark.asyncio
async def test_cancel_marks_active_task_cancelled_and_is_idempotent():
    model = UnderstandingModel(
        ["swap_quote"],
        swap=[SwapSlotPatch(source_symbol="USDC", destination_symbol="USDT", input_amount="1")],
    )
    graph = build_graph(model=model, providers=[Provider()])
    config = {"configurable": {"thread_id": "task-cancel"}}
    await graph.ainvoke(turn("用 1 USDC 换 USDT"), config=config)

    first = await graph.ainvoke(turn("取消兑换"), config=config)
    second = await graph.ainvoke(turn("取消兑换"), config=config)

    assert first["response"]["kind"] == "cancelled"
    assert second["response"]["kind"] == "cancelled"
    assert second["active_task"]["status"] == "cancelled"
    assert second["active_task"]["revision"] == first["active_task"]["revision"]


@pytest.mark.asyncio
async def test_cancel_understands_natural_abandon_message():
    model = UnderstandingModel(
        ["swap_quote"],
        swap=[SwapSlotPatch(source_symbol="USDC", destination_symbol="USDT", input_amount="1")],
    )
    graph = build_graph(model=model, providers=[Provider()])
    config = {"configurable": {"thread_id": "task-natural-cancel"}}
    await graph.ainvoke(turn("用 1 USDC 换 USDT"), config=config)

    result = await graph.ainvoke(turn("算了，放弃吧"), config=config)

    assert result["response"] == {"kind": "cancelled", "message": "已取消当前swap任务。"}
    assert result["active_task"]["status"] == "cancelled"
    assert result["conversation_state"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_sqlite_restart_after_clarification_accepts_next_slot_patch(tmp_path):
    db_path = tmp_path / "task-memory.db"
    config = {"configurable": {"thread_id": "task-restart"}}
    first_handle = create_checkpointer(f"sqlite:///{db_path}")
    try:
        first_graph = build_graph(
            model=UnderstandingModel(
                ["transfer"],
                transfer=[TransferSlotPatch(chain="BASE", symbol="ETH", amount="0.01")],
            ),
            chains={"BASE": Chain()},
            checkpointer=first_handle.checkpointer,
        )
        first = await first_graph.ainvoke(turn("转 0.01 ETH"), config=config)
        assert first["response"]["missing_fields"] == ["transfer_recipient"]
    finally:
        await first_handle.aclose()

    second_handle = create_checkpointer(f"sqlite:///{db_path}")
    try:
        second_graph = build_graph(
            model=UnderstandingModel(
                ["clarification"],
                transfer=[TransferSlotPatch(recipient=RECIPIENT)],
            ),
            chains={"BASE": Chain()},
            checkpointer=second_handle.checkpointer,
        )
        second = await second_graph.ainvoke(turn(f"收款地址是 {RECIPIENT}"), config=config)

        assert second["response"]["kind"] == "transfer_prepare"
        assert second["pending_transaction"]["to"] == RECIPIENT
        assert second["active_task"]["revision"] == 2
    finally:
        await second_handle.aclose()
