import asyncio
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from langgraph.types import Command

from wallet_agent.api import create_app
from wallet_agent.chains.registry import ChainAdapterRegistry
from wallet_agent.domain.models import (
    AllowanceRequirement,
    Asset,
    FeeEstimate,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
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

    def __init__(self):
        self.prepare_calls = 0
        self.status_calls = 0

    async def prepare(self, quote):
        self.prepare_calls += 1
        return UnsignedTransaction(
            chain=quote.source_asset.chain, to="0x" + "4" * 40, data="0xabc", value="0"
        )

    async def register_broadcast(self, provider_reference, tx_hash):
        return ProviderOrder(
            provider="bridgers",
            provider_order_id="order-1",
            provider_reference=provider_reference,
            tx_hash=tx_hash,
        )

    async def get_status(self, order):
        self.status_calls += 1
        return NormalizedOrderStatus(
            provider="bridgers",
            provider_order_id=order.provider_order_id,
            provider_reference=order.provider_reference,
            status="processing",
            tx_hash=order.tx_hash,
        )


class StatusModel(Model):
    async def ainvoke(self, value):
        if value.get("message") == "怎么样了？":
            return {"intent": "swap_status"}
        return await super().ainvoke(value)


def authorization_quote(reference: str) -> NormalizedQuote:
    token = Asset(
        chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x" + "3" * 40
    )
    return NormalizedQuote(
        provider="bridgers",
        source_asset=token,
        destination_asset=Asset(
            chain="BSC", symbol="USDT", decimals=6, address="0x" + "5" * 40
        ),
        input_amount=Decimal("1"),
        input_amount_raw="100",
        expected_output=Decimal("1"),
        expected_output_raw="100",
        provider_reference=reference,
        allowance_requirement=AllowanceRequirement(
            token=token,
            owner="0x" + "1" * 40,
            spender="0x" + "4" * 40,
            required_amount_raw="100",
            current_allowance_raw="0",
        ),
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


@pytest.mark.asyncio
async def test_status_turn_after_broadcast_polls_order_without_reopening_approval():
    token = Asset(
        chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x" + "3" * 40
    )
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
        destination_asset=Asset(
            chain="BSC", symbol="USDT", decimals=6, address="0x" + "5" * 40
        ),
        input_amount=Decimal("1"),
        input_amount_raw="100",
        expected_output=Decimal("1"),
        expected_output_raw="100",
        provider_reference="ref-status",
        allowance_requirement=requirement,
    )
    adapter = Adapter()
    provider = Provider()
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-status",
            user_id="alice",
            thread_id="t-status",
            quote_candidates=[quote],
        )
    )
    graph = build_graph(model=StatusModel(), providers=[provider], chains={"BASE": adapter})
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
            "/v1/swap/s-status/select-quote",
            json={"user_id": "alice", "provider_reference": "ref-status"},
        )
        await client.post(
            "/v1/swap/s-status/approve-broadcast",
            json={
                "user_id": "alice",
                "chain": "BASE",
                "approve_tx_hash": "0x" + "a" * 64,
            },
        )
        adapter.allowance = "100"
        await client.post("/v1/swap/s-status/continue", json={"user_id": "alice"})
        broadcast = await client.post(
            "/v1/swap/s-status/broadcast",
            json={"user_id": "alice", "chain": "BASE", "tx_hash": "0x" + "b" * 64},
        )
        turn = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "conversation_id": "t-status",
                "session_id": "s-status",
                "message": "怎么样了？",
            },
        )
        await app.state.runs[turn.json()["run_id"]]["task"]

    run = app.state.runs[turn.json()["run_id"]]
    final_state = next(
        event["state"]
        for event in reversed(run["events"])
        if event["event"] == "complete"
    )
    session = await store.get("s-status")

    assert broadcast.status_code == 200
    assert broadcast.json()["stage"] == "broadcasted"
    assert provider.status_calls == 1
    assert final_state["response"]["kind"] == "swap_status"
    assert final_state["response"]["status"]["status"] == "processing"
    assert final_state.get("approval_transaction") is None
    assert final_state.get("pending_transaction") is None
    assert session is not None
    assert session.status == "processing"
    assert session.stage == "processing"
    assert session.order_status is not None
    assert session.order_status.status == "processing"


@pytest.mark.asyncio
async def test_status_turn_during_pending_approval_reports_approval_phase():
    class PendingAdapter(Adapter):
        async def get_transaction_receipt(self, _hash):
            return None

    quote = authorization_quote("ref-approval-pending")
    adapter = PendingAdapter()
    provider = Provider()
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-approval-pending",
            user_id="alice",
            thread_id="t-approval-pending",
            quote_candidates=[quote],
        )
    )
    graph = build_graph(model=StatusModel(), providers=[provider], chains={"BASE": adapter})
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
            "/v1/swap/s-approval-pending/select-quote",
            json={"user_id": "alice", "provider_reference": quote.provider_reference},
        )
        await client.post(
            "/v1/swap/s-approval-pending/approve-broadcast",
            json={
                "user_id": "alice",
                "chain": "BASE",
                "approve_tx_hash": "0x" + "a" * 64,
            },
        )
        await client.post(
            "/v1/swap/s-approval-pending/continue", json={"user_id": "alice"}
        )
        turn = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "conversation_id": "t-approval-pending",
                "session_id": "s-approval-pending",
                "message": "怎么样了？",
            },
        )
        await app.state.runs[turn.json()["run_id"]]["task"]

    run = app.state.runs[turn.json()["run_id"]]
    final_state = next(
        event["state"]
        for event in reversed(run["events"])
        if event["event"] == "complete"
    )
    session = await store.get("s-approval-pending")

    assert final_state["response"]["kind"] == "swap_status"
    assert final_state["response"]["status"] == "approval_pending"
    assert session is not None
    assert session.status == "approval_pending"
    assert session.stage == "approval_pending"
    assert session.approval_transaction is not None
    assert session.provider_order is None


@pytest.mark.asyncio
async def test_status_turn_after_approval_confirmation_prepares_swap():
    class ConfirmingAdapter(Adapter):
        def __init__(self):
            super().__init__()
            self.receipt = None

        async def get_transaction_receipt(self, _hash):
            return self.receipt

    quote = authorization_quote("ref-approval-confirmed")
    adapter = ConfirmingAdapter()
    provider = Provider()
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-approval-confirmed",
            user_id="alice",
            thread_id="t-approval-confirmed",
            quote_candidates=[quote],
        )
    )
    graph = build_graph(model=StatusModel(), providers=[provider], chains={"BASE": adapter})
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
            "/v1/swap/s-approval-confirmed/select-quote",
            json={"user_id": "alice", "provider_reference": quote.provider_reference},
        )
        await client.post(
            "/v1/swap/s-approval-confirmed/approve-broadcast",
            json={
                "user_id": "alice",
                "chain": "BASE",
                "approve_tx_hash": "0x" + "a" * 64,
            },
        )
        await client.post(
            "/v1/swap/s-approval-confirmed/continue", json={"user_id": "alice"}
        )
        adapter.receipt = {"status": "0x1"}
        adapter.allowance = "100"
        turn = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "conversation_id": "t-approval-confirmed",
                "session_id": "s-approval-confirmed",
                "message": "怎么样了？",
            },
        )
        await app.state.runs[turn.json()["run_id"]]["task"]

    run = app.state.runs[turn.json()["run_id"]]
    final_state = next(
        event["state"]
        for event in reversed(run["events"])
        if event["event"] == "complete"
    )
    session = await store.get("s-approval-confirmed")

    assert final_state["response"]["kind"] == "swap_status"
    assert final_state["response"]["status"] == "swap_ready"
    assert final_state["pending_transaction"]["data"] == "0xabc"
    assert session is not None
    assert session.status == "swap_ready"
    assert session.stage == "swap_ready"
    assert session.pending_transaction is not None


@pytest.mark.asyncio
async def test_status_turn_while_waiting_for_swap_signature_stays_swap_ready():
    quote = authorization_quote("ref-waiting-for-signature")
    pending_transaction = UnsignedTransaction(
        chain="BASE", to="0x" + "4" * 40, data="0xabc", value="0"
    )
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-waiting-for-signature",
            user_id="alice",
            thread_id="t-waiting-for-signature",
            status="swap_ready",
            stage="swap_ready",
            quote=quote,
            pending_transaction=pending_transaction,
        )
    )
    graph = build_graph(model=StatusModel(), providers=[Provider()], chains={})
    app = create_app(graph=graph, store=store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        turn = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "conversation_id": "t-waiting-for-signature",
                "session_id": "s-waiting-for-signature",
                "message": "怎么样了？",
            },
        )
        await app.state.runs[turn.json()["run_id"]]["task"]

    run = app.state.runs[turn.json()["run_id"]]
    final_state = next(
        event["state"]
        for event in reversed(run["events"])
        if event["event"] == "complete"
    )
    session = await store.get("s-waiting-for-signature")

    assert final_state["response"]["kind"] == "swap_status"
    assert final_state["response"]["status"] == "swap_ready"
    assert final_state["response"]["pending_transaction"]["data"] == "0xabc"
    assert session is not None
    assert session.status == "swap_ready"
    assert session.pending_transaction is not None


@pytest.mark.asyncio
async def test_continue_does_not_resume_an_unrelated_graph_task():
    quote = authorization_quote("ref-unrelated-task")
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-unrelated-task",
            user_id="alice",
            thread_id="t-unrelated-task",
            quote=quote,
            approval_tx_hash="0x" + "a" * 64,
        )
    )

    class Graph:
        async def aget_state(self, _config):
            return SimpleNamespace(
                tasks=(SimpleNamespace(interrupts=()),),
                next=("status_poll",),
                values={},
            )

        async def ainvoke(self, value, config):
            del config
            if isinstance(value, Command):
                return {"response": {"stage": "wrong_resume"}}
            return {
                "authorization_stage": "swap_ready",
                "pending_transaction": UnsignedTransaction(
                    chain="BASE", to="0x" + "4" * 40, data="0xabc", value="0"
                ).model_dump(mode="json"),
                "response": {"stage": "swap_ready"},
            }

    app = create_app(graph=Graph(), store=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/swap/s-unrelated-task/continue", json={"user_id": "alice"}
        )

    assert response.status_code == 200
    assert response.json()["stage"] == "swap_ready"
    assert response.json()["pending_transaction"]["data"] == "0xabc"


@pytest.mark.asyncio
async def test_continue_does_not_mutate_an_unrelated_graph_interrupt():
    quote = authorization_quote("ref-unrelated-interrupt")
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-unrelated-interrupt",
            user_id="alice",
            thread_id="t-unrelated-interrupt",
            stage="confirmation_required",
            quote=quote,
            approval_tx_hash="0x" + "a" * 64,
        )
    )

    class Graph:
        def __init__(self):
            self.invoked = False

        async def aget_state(self, _config):
            return SimpleNamespace(
                tasks=(
                    SimpleNamespace(
                        interrupts=(
                            SimpleNamespace(value={"kind": "confirmation_required"}),
                        )
                    ),
                ),
                next=("confirmation_wait",),
                values={},
            )

        async def ainvoke(self, _value, config):
            del config
            self.invoked = True
            return {"response": {"stage": "wrong_mutation"}}

    graph = Graph()
    app = create_app(graph=graph, store=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/swap/s-unrelated-interrupt/continue", json={"user_id": "alice"}
        )

    assert response.status_code == 200
    assert response.json()["stage"] == "confirmation_required"
    assert graph.invoked is False


@pytest.mark.asyncio
async def test_turn_and_continue_are_serialized_for_the_same_thread():
    quote = authorization_quote("ref-serialized")
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-serialized",
            user_id="alice",
            thread_id="t-serialized",
            quote=quote,
            approval_tx_hash="0x" + "a" * 64,
        )
    )

    class Graph:
        def __init__(self):
            self.active = False
            self.overlapped = False
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def aget_state(self, _config):
            return SimpleNamespace(tasks=(), next=(), values={})

        async def astream(self, _value, config, stream_mode):
            del config, stream_mode
            self.active = True
            self.started.set()
            await self.release.wait()
            self.active = False
            yield {"response": {"kind": "clarification"}}

        async def ainvoke(self, _value, config):
            del config
            self.overlapped = self.overlapped or self.active
            return {
                "authorization_stage": "approval_pending",
                "response": {"stage": "approval_pending"},
            }

    graph = Graph()
    app = create_app(graph=graph, store=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        turn = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "conversation_id": "t-serialized",
                "session_id": "s-serialized",
                "message": "怎么样了？",
            },
        )
        run_task = app.state.runs[turn.json()["run_id"]]["task"]
        await graph.started.wait()
        continue_task = asyncio.create_task(
            client.post("/v1/swap/s-serialized/continue", json={"user_id": "alice"})
        )
        await asyncio.sleep(0)
        graph.release.set()
        continued = await continue_task
        await run_task

    assert continued.status_code == 200
    assert graph.overlapped is False
