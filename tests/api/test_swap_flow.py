from decimal import Decimal

import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.chains.registry import ChainAdapterRegistry
from wallet_agent.domain.models import (
    Asset,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    UnsignedTransaction,
)
from wallet_agent.graph.build import build_graph
from wallet_agent.persistence import InMemorySessionStore


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
async def test_broadcast_rejects_hash_missing_from_chain_before_provider_registration():
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

    assert broadcast.status_code == 409
    assert broadcast.json()["code"] == "TRANSACTION_NOT_FOUND"
    assert provider.broadcast_calls == 0


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
