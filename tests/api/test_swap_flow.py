from decimal import Decimal

import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.chains.registry import ChainAdapterRegistry
from wallet_agent.domain.models import (
    Asset,
    DepositOrder,
    FeeEstimate,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    UnsignedTransaction,
)
from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import _deposit_order_to_transaction
from wallet_agent.persistence import InMemorySessionStore, SwapSessionRecord


class FakeModel:
    async def ainvoke(self, _value):
        return {"intent": "swap_quote"}


class FakeProvider:
    provider_name = "bridgers"

    def __init__(self):
        self.quote_calls = 0
        self.prepare_calls = 0
        self.broadcast_calls = 0

    async def quote(self, request):
        self.quote_calls += 1
        return NormalizedQuote(
            provider="bridgers",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal("9"),
            expected_output_raw="9000000",
            provider_reference="quote-ref",
        )

    async def prepare(self, quote):
        self.prepare_calls += 1
        return UnsignedTransaction(
            chain=quote.source_asset.chain,
            to="0xrouter",
            data="0xdata",
            value="0x0",
            provider="bridgers",
            provider_reference=quote.provider_reference,
        )

    async def register_broadcast(self, provider_reference, tx_hash):
        self.broadcast_calls += 1
        return ProviderOrder(
            provider="bridgers",
            provider_order_id="order-1",
            provider_reference=provider_reference,
            tx_hash=tx_hash,
        )

    async def get_status(self, order):
        return NormalizedOrderStatus(
            provider="bridgers",
            provider_order_id=order.provider_order_id,
            provider_reference=order.provider_reference,
            status="processing",
            tx_hash=order.tx_hash,
        )


def request_payload():
    request = SwapQuoteRequest(
        source_asset=Asset(chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x1"),
        destination_asset=Asset(chain="BSC", chain_id=56, symbol="USDT", decimals=6, address="0x2"),
        input_amount=Decimal("10"),
        input_amount_raw="10000000",
        sender_address="0xsender",
        recipient_address="0xrecipient",
    )
    return {
        "conversation_id": "conversation-1",
        "user_id": "alice",
        "message": "swap",
        "metadata": {"swap_request": request.model_dump(mode="json")},
    }


async def make_client():
    provider = FakeProvider()
    graph = build_graph(model=FakeModel(), providers=[provider])
    app = create_app(graph=graph, providers={"bridgers": provider}, store=InMemorySessionStore())
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return app, client, provider


@pytest.mark.asyncio
async def test_turn_quote_interrupt_confirm_prepare_and_session_projection():
    app, client, provider = await make_client()
    async with client:
        response = await client.post("/v1/agent/turn", json=request_payload())
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"]
        await app.state.runs[body["run_id"]]["task"]

        session_id = body["session_id"]
        session = (await client.get(f"/v1/swap/{session_id}", params={"user_id": "alice"})).json()
        assert session["quote"]["provider"] == "bridgers"
        assert session["status"] == "awaiting_confirmation"

        confirmed = await client.post(
            f"/v1/swap/{session_id}/confirm", json={"user_id": "alice", "approved": True}
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["pending_transaction"]["to"] == "0xrouter"
        assert provider.prepare_calls == 1


@pytest.mark.asyncio
async def test_select_quote_requires_confirmation_before_no_approval_prepare():
    provider = FakeProvider()
    quote = NormalizedQuote(
        provider="bridgers",
        source_asset=Asset(
            chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address="0x" + "1" * 40
        ),
        destination_asset=Asset(
            chain="BSC", chain_id=56, symbol="USDT", decimals=6, address="0x" + "2" * 40
        ),
        input_amount=Decimal("10"),
        input_amount_raw="10000000",
        expected_output=Decimal("9"),
        expected_output_raw="9000000",
        provider_reference="quote-confirm-first",
    )
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="s-no-approval-confirm",
            user_id="alice",
            thread_id="t-no-approval-confirm",
            quote_candidates=[quote],
        )
    )
    graph = build_graph(model=FakeModel(), providers=[provider])
    app = create_app(
        graph=graph,
        providers={"bridgers": provider},
        store=store,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        selected = await client.post(
            "/v1/swap/s-no-approval-confirm/select-quote",
            json={"user_id": "alice", "provider_reference": quote.provider_reference},
        )
        assert selected.status_code == 200
        assert selected.json()["status"] == "awaiting_confirmation"
        assert provider.prepare_calls == 0

        confirmed = await client.post(
            "/v1/swap/s-no-approval-confirm/confirm",
            json={"user_id": "alice", "approved": True},
        )

    assert confirmed.status_code == 200
    assert confirmed.json()["stage"] == "swap_ready"
    assert provider.prepare_calls == 1


@pytest.mark.asyncio
async def test_broadcast_is_idempotent_and_conflicting_hash_is_rejected():
    app, client, provider = await make_client()
    async with client:
        response = await client.post("/v1/agent/turn", json=request_payload())
        body = response.json()
        await app.state.runs[body["run_id"]]["task"]
        session_id = body["session_id"]
        await client.post(
            f"/v1/swap/{session_id}/confirm", json={"user_id": "alice", "approved": True}
        )
        tx_hash = "0x" + "a" * 64
        first = await client.post(
            f"/v1/swap/{session_id}/broadcast",
            json={"user_id": "alice", "chain": "BASE", "tx_hash": tx_hash},
        )
        second = await client.post(
            f"/v1/swap/{session_id}/broadcast",
            json={"user_id": "alice", "chain": "BASE", "tx_hash": tx_hash},
        )
        conflict = await client.post(
            f"/v1/swap/{session_id}/broadcast",
            json={"user_id": "alice", "chain": "BASE", "tx_hash": "0x" + "b" * 64},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert conflict.status_code == 409
        assert provider.broadcast_calls == 1


@pytest.mark.asyncio
async def test_broadcast_accepts_hash_temporarily_missing_from_chain():
    class BroadcastAdapter:
        async def get_transaction_receipt(self, _tx_hash):
            return None

        async def get_transaction(self, _tx_hash):
            return None

    app, client, provider = await make_client()
    app.state.chain_registry = ChainAdapterRegistry({"BASE": BroadcastAdapter()})
    async with client:
        response = await client.post("/v1/agent/turn", json=request_payload())
        body = response.json()
        await app.state.runs[body["run_id"]]["task"]
        session_id = body["session_id"]
        await client.post(
            f"/v1/swap/{session_id}/confirm", json={"user_id": "alice", "approved": True}
        )
        tx_hash = "0x" + "b" * 64
        broadcast = await client.post(
            f"/v1/swap/{session_id}/broadcast",
            json={"user_id": "alice", "chain": "BASE", "tx_hash": tx_hash},
        )

    assert broadcast.status_code == 200
    assert broadcast.json()["status"] == "broadcast_pending"
    assert broadcast.json()["broadcast_tx_hash"] == tx_hash
    assert provider.broadcast_calls == 1


@pytest.mark.asyncio
async def test_broadcast_retries_rpc_visibility_before_marking_pending():
    class EventuallyVisibleAdapter:
        def __init__(self):
            self.receipt_calls = 0
            self.transaction_calls = 0

        async def get_transaction_receipt(self, _tx_hash):
            self.receipt_calls += 1
            return None

        async def get_transaction(self, _tx_hash):
            self.transaction_calls += 1
            if self.transaction_calls < 2:
                return None
            return {"hash": _tx_hash}

    app, _client, provider = await make_client()
    adapter = EventuallyVisibleAdapter()
    app.state.chain_registry = ChainAdapterRegistry({"BASE": adapter})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/turn", json=request_payload())
        body = response.json()
        await app.state.runs[body["run_id"]]["task"]
        session_id = body["session_id"]
        await client.post(
            f"/v1/swap/{session_id}/confirm", json={"user_id": "alice", "approved": True}
        )
        tx_hash = "0x" + "c" * 64
        broadcast = await client.post(
            f"/v1/swap/{session_id}/broadcast",
            json={"user_id": "alice", "chain": "BASE", "tx_hash": tx_hash},
        )

    assert broadcast.status_code == 200
    assert broadcast.json()["status"] == "broadcast_pending"
    assert adapter.receipt_calls == 2
    assert adapter.transaction_calls == 2


@pytest.mark.asyncio
async def test_broadcast_rejects_explicitly_failed_receipt():
    class FailedReceiptAdapter:
        async def get_transaction_receipt(self, _tx_hash):
            return {"status": "0x0"}

    app, client, provider = await make_client()
    app.state.chain_registry = ChainAdapterRegistry({"BASE": FailedReceiptAdapter()})
    async with client:
        response = await client.post("/v1/agent/turn", json=request_payload())
        body = response.json()
        await app.state.runs[body["run_id"]]["task"]
        session_id = body["session_id"]
        await client.post(
            f"/v1/swap/{session_id}/confirm", json={"user_id": "alice", "approved": True}
        )
        broadcast = await client.post(
            f"/v1/swap/{session_id}/broadcast",
            json={"user_id": "alice", "chain": "BASE", "tx_hash": "0x" + "d" * 64},
        )

    assert broadcast.status_code == 409
    assert broadcast.json()["code"] == "TRANSACTION_FAILED"
    assert provider.broadcast_calls == 0


@pytest.mark.asyncio
async def test_omnibridge_deposit_order_becomes_wallet_transaction_and_registers_order_id():
    source = Asset(
        chain="ETH", chain_id=1, symbol="USDC", decimals=6, address="0x" + "1" * 40
    )
    destination = Asset(
        chain="ETH", chain_id=1, symbol="USDT", decimals=6, address="0x" + "2" * 40
    )
    sender = "0x" + "3" * 40
    deposit_address = "0x" + "4" * 40

    class DepositAdapter:
        def __init__(self):
            self.registered_receipt = {"status": "0x1"}

        def build_erc20_transfer(self, *, token, from_address, to_address, amount_raw):
            return UnsignedTransaction(
                chain=token.chain,
                chain_id=token.chain_id,
                to=token.address,
                data=f"transfer:{from_address}:{to_address}:{amount_raw}",
                value="0",
            )

        async def estimate_fee(self, *, to=None, data=None, from_address=None, value=None):
            return FeeEstimate(
                chain="ETH",
                chain_id=1,
                asset=Asset(chain="ETH", chain_id=1, symbol="ETH", decimals=18),
                amount=Decimal("0.000004"),
                amount_raw="4000000000000",
                gas_limit="50000",
                max_fee_per_gas="100000000",
                max_priority_fee_per_gas="2000000",
            )

        async def get_transaction_receipt(self, _tx_hash):
            return self.registered_receipt

    class DepositProvider:
        provider_name = "omnibridge"

        def __init__(self):
            self.registered_reference = None

        async def quote(self, request):
            return NormalizedQuote(
                provider="omnibridge",
                source_asset=request.source_asset,
                destination_asset=request.destination_asset,
                input_amount=request.input_amount,
                input_amount_raw=request.input_amount_raw,
                expected_output=Decimal("9"),
                expected_output_raw="9000000",
                provider_reference="quote-hash",
            )

        async def prepare(self, quote):
            return DepositOrder(
                provider="omnibridge",
                provider_order_id="order-omni",
                deposit_address=deposit_address,
                source_asset=quote.source_asset,
                destination_asset=quote.destination_asset,
                input_amount=quote.input_amount,
                input_amount_raw=quote.input_amount_raw,
                recipient_address=sender,
                provider_reference="order-omni",
            )

        async def register_broadcast(self, provider_reference, tx_hash):
            self.registered_reference = provider_reference
            return ProviderOrder(
                provider="omnibridge",
                provider_order_id=provider_reference,
                provider_reference=provider_reference,
                tx_hash=tx_hash,
            )

    adapter = DepositAdapter()
    provider = DepositProvider()
    store = InMemorySessionStore()
    graph = build_graph(model=FakeModel(), providers=[provider], chains={"ETH": adapter})
    app = create_app(
        graph=graph,
        providers={"omnibridge": provider},
        chain_registry=ChainAdapterRegistry({"ETH": adapter}),
        store=store,
    )
    request = SwapQuoteRequest(
        source_asset=source,
        destination_asset=destination,
        input_amount=Decimal("9.5"),
        input_amount_raw="9500000",
        sender_address=sender,
        recipient_address=sender,
    )
    payload = {
        "conversation_id": "omni-conversation",
        "user_id": "alice",
        "message": "swap",
        "metadata": {"swap_request": request.model_dump(mode="json")},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/agent/turn", json=payload)
        body = response.json()
        await app.state.runs[body["run_id"]]["task"]
        session_id = body["session_id"]
        prepared = await client.post(
            f"/v1/swap/{session_id}/confirm",
            json={"user_id": "alice", "approved": True},
        )
        transaction = prepared.json()["pending_transaction"]
        broadcast = await client.post(
            f"/v1/swap/{session_id}/broadcast",
            json={"user_id": "alice", "chain": "ETH", "tx_hash": "0x" + "e" * 64},
        )

    assert transaction["to"] == source.address
    assert transaction["data"] == f"transfer:{sender}:{deposit_address}:9500000"
    assert transaction["value"] == "0"
    assert transaction["gas_limit"] == "50000"
    assert transaction["max_fee_per_gas"] == "100000000"
    assert transaction["max_priority_fee_per_gas"] == "2000000"
    assert transaction["provider_reference"] == "order-omni"
    assert broadcast.status_code == 200
    assert provider.registered_reference == "order-omni"


def test_omnibridge_native_deposit_order_becomes_native_wallet_transaction():
    source = Asset(chain="ETH", chain_id=1, symbol="ETH", decimals=18)
    destination = Asset(
        chain="ETH", chain_id=1, symbol="USDT", decimals=6, address="0x" + "2" * 40
    )
    sender = "0x" + "3" * 40
    deposit_address = "0x" + "4" * 40

    class NativeDepositAdapter:
        def build_native_transfer(self, *, from_address, to_address, amount_raw):
            return UnsignedTransaction(
                chain="ETH",
                chain_id=1,
                to=to_address,
                data="0x",
                value=amount_raw,
                display={"from": from_address},
            )

    transaction = _deposit_order_to_transaction(
        DepositOrder(
            provider="omnibridge",
            provider_order_id="order-native",
            deposit_address=deposit_address,
            source_asset=source,
            destination_asset=destination,
            input_amount=Decimal("0.01"),
            input_amount_raw="10000000000000000",
            recipient_address=sender,
            provider_reference="order-native",
        ),
        adapter=NativeDepositAdapter(),
        sender=sender,
    )

    assert transaction.to == deposit_address
    assert transaction.data == "0x"
    assert transaction.value == "10000000000000000"
    assert transaction.provider_reference == "order-native"
    assert transaction.display["provider_order_id"] == "order-native"


@pytest.mark.asyncio
async def test_swap_session_is_owner_scoped():
    app, client, _provider = await make_client()
    async with client:
        response = await client.post("/v1/agent/turn", json=request_payload())
        body = response.json()
        await app.state.runs[body["run_id"]]["task"]
        denied = await client.get(
            f"/v1/swap/{body['session_id']}", params={"user_id": "mallory"}
        )
        assert denied.status_code == 404
