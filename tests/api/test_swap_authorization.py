from decimal import Decimal

import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.chains.registry import ChainAdapterRegistry
from wallet_agent.domain.models import (
    AllowanceRequirement,
    Asset,
    FeeEstimate,
    NormalizedQuote,
    UnsignedTransaction,
)
from wallet_agent.graph.build import build_graph
from wallet_agent.persistence import InMemorySessionStore, SwapSessionRecord


class Model:
    async def ainvoke(self, value):
        return {"intent": value.get("intent", "clarification")}


class Adapter:
    def __init__(self):
        self.allowance = "0"

    async def get_allowance(self, *_args):
        return self.allowance

    def build_erc20_approve(self, *, token, owner, spender, amount_raw):
        return UnsignedTransaction(
            chain=token.chain,
            chain_id=token.chain_id,
            to=token.address,
            data="0x095ea7b3" + "0" * 128,
            value="0",
        )

    async def get_transaction_receipt(self, _hash):
        return {"status": "0x1"}


class Provider:
    provider_name = "bridgers"
    prepare_calls = 0

    async def prepare(self, quote):
        self.prepare_calls += 1
        return UnsignedTransaction(
            chain=quote.source_asset.chain, to="0x" + "4" * 40, data="0xabc", value="0"
        )


@pytest.mark.asyncio
async def test_select_quote_returns_approval_and_continue_prepares_swap():
    token = Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x" + "3" * 40)
    requirement = AllowanceRequirement(
        token=token,
        owner="0x" + "1" * 40,
        spender="0x" + "4" * 40,
        required_amount_raw="100",
        current_allowance_raw="0",
    )
    quote = NormalizedQuote(
        provider="bridgers",
        source_asset=token,
        destination_asset=Asset(chain="BSC", symbol="USDT", decimals=6, address="0x" + "5" * 40),
        input_amount=Decimal("1"),
        input_amount_raw="100",
        expected_output=Decimal("1"),
        expected_output_raw="100",
        provider_reference="ref-1",
        allowance_requirement=requirement,
    )
    adapter = Adapter()
    provider = Provider()
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s1",
            user_id="alice",
            thread_id="t1",
            quote_candidates=[quote],
        )
    )
    graph = build_graph(model=Model(), providers=[provider], chains={"BASE": adapter})
    app = create_app(
        graph=graph,
        providers={"bridgers": provider},
        chain_registry=ChainAdapterRegistry({"BASE": adapter}),
        store=store,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        selected = await client.post(
            "/v1/swap/s1/select-quote", json={"user_id": "alice", "provider_reference": "ref-1"}
        )
        assert selected.status_code == 200
        assert selected.json()["approval_transaction"]["data"].startswith("0x095ea7b3")
        await client.post(
            "/v1/swap/s1/approve-broadcast",
            json={"user_id": "alice", "chain": "BASE", "approve_tx_hash": "0x" + "a" * 64},
        )
        adapter.allowance = "100"
        continued = await client.post("/v1/swap/s1/continue", json={"user_id": "alice"})
    assert continued.status_code == 200
    assert continued.json()["pending_transaction"]["data"] == "0xabc"
    assert provider.prepare_calls == 1


@pytest.mark.asyncio
async def test_continue_rechecks_pending_approval_until_receipt_confirms():
    token = Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x" + "3" * 40)
    requirement = AllowanceRequirement(
        token=token,
        owner="0x" + "1" * 40,
        spender="0x" + "4" * 40,
        required_amount_raw="100",
        current_allowance_raw="0",
    )
    quote = NormalizedQuote(
        provider="bridgers",
        source_asset=token,
        destination_asset=Asset(chain="BSC", symbol="USDT", decimals=6, address="0x" + "5" * 40),
        input_amount=Decimal("1"),
        input_amount_raw="100",
        expected_output=Decimal("1"),
        expected_output_raw="100",
        provider_reference="ref-pending",
        allowance_requirement=requirement,
    )

    class PollingAdapter(Adapter):
        def __init__(self):
            super().__init__()
            self.receipt = None

        async def get_transaction_receipt(self, _hash):
            return self.receipt

    adapter = PollingAdapter()
    provider = Provider()
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-pending",
            user_id="alice",
            thread_id="t-pending",
            quote_candidates=[quote],
        )
    )
    graph = build_graph(model=Model(), providers=[provider], chains={"BASE": adapter})
    app = create_app(
        graph=graph,
        providers={"bridgers": provider},
        chain_registry=ChainAdapterRegistry({"BASE": adapter}),
        store=store,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        selected = await client.post(
            "/v1/swap/s-pending/select-quote",
            json={"user_id": "alice", "provider_reference": "ref-pending"},
        )
        assert selected.status_code == 200
        await client.post(
            "/v1/swap/s-pending/approve-broadcast",
            json={"user_id": "alice", "chain": "BASE", "approve_tx_hash": "0x" + "a" * 64},
        )
        pending = await client.post("/v1/swap/s-pending/continue", json={"user_id": "alice"})
        assert pending.status_code == 200
        assert pending.json()["stage"] == "approval_pending"

        adapter.receipt = {"status": "0x1"}
        adapter.allowance = "100"
        continued = await client.post(
            "/v1/swap/s-pending/continue", json={"user_id": "alice"}
        )

    assert continued.status_code == 200
    assert continued.json()["pending_transaction"]["data"] == "0xabc"


@pytest.mark.asyncio
async def test_swap_transaction_includes_contextual_gas_estimate():
    class FeeAdapter(Adapter):
        def __init__(self):
            super().__init__()
            self.estimate_args = []

        async def estimate_fee(self, *, to=None, data=None, from_address=None, value=None):
            self.estimate_args.append((to, data, from_address, value))
            return FeeEstimate(
                chain="BASE",
                chain_id=8453,
                asset=Asset(chain="BASE", chain_id=8453, symbol="ETH", decimals=18),
                amount=Decimal("0.000003"),
                amount_raw="3000000000000",
                gas_limit="30000",
                max_fee_per_gas="100000000",
                max_priority_fee_per_gas="2000000",
            )

    token = Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x" + "3" * 40)
    requirement = AllowanceRequirement(
        token=token,
        owner="0x" + "1" * 40,
        spender="0x" + "4" * 40,
        required_amount_raw="100",
        current_allowance_raw="0",
    )
    quote = NormalizedQuote(
        provider="bridgers",
        source_asset=token,
        destination_asset=Asset(chain="BSC", symbol="USDT", decimals=6, address="0x" + "5" * 40),
        input_amount=Decimal("1"),
        input_amount_raw="100",
        expected_output=Decimal("1"),
        expected_output_raw="100",
        provider_reference="ref-gas",
        allowance_requirement=requirement,
    )
    adapter = FeeAdapter()
    provider = Provider()
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-gas",
            user_id="alice",
            thread_id="t-gas",
            quote_candidates=[quote],
        )
    )
    graph = build_graph(model=Model(), providers=[provider], chains={"BASE": adapter})
    app = create_app(
        graph=graph,
        providers={"bridgers": provider},
        chain_registry=ChainAdapterRegistry({"BASE": adapter}),
        store=store,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/v1/swap/s-gas/select-quote",
            json={"user_id": "alice", "provider_reference": "ref-gas"},
        )
        await client.post(
            "/v1/swap/s-gas/approve-broadcast",
            json={"user_id": "alice", "chain": "BASE", "approve_tx_hash": "0x" + "a" * 64},
        )
        adapter.receipt = {"status": "0x1"}
        adapter.allowance = "100"
        response = await client.post("/v1/swap/s-gas/continue", json={"user_id": "alice"})

    transaction = response.json()["pending_transaction"]
    assert adapter.estimate_args[-1] == ("0x" + "4" * 40, "0xabc", "0x" + "1" * 40, "0")
    assert transaction["gas_limit"] == "30000"
    assert transaction["max_fee_per_gas"] == "100000000"
    assert transaction["max_priority_fee_per_gas"] == "2000000"
