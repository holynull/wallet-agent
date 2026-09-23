from decimal import Decimal

import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.domain.models import Asset, TokenBalance, UnsignedTransaction
from wallet_agent.graph.build import build_graph
from wallet_agent.persistence import InMemorySessionStore, SwapSessionRecord


class Model:
    async def ainvoke(self, value):
        return {"intent": value.get("intent", "clarification")}


class Adapter:
    async def get_transaction_receipt(self, _tx_hash):
        return None

    async def get_transaction(self, tx_hash):
        return {"hash": tx_hash}

    async def get_native_balance(self, _address):
        return TokenBalance(
            asset=Asset(chain="BASE", symbol="ETH", decimals=18),
            amount=Decimal("2"),
            amount_raw="200",
        )

    def build_native_transfer(self, *, from_address, to_address, amount_raw):
        return UnsignedTransaction(
            chain="BASE",
            to=to_address,
            data="0x",
            value=amount_raw,
            display={"from": from_address},
        )


class Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get_adapter(self, _chain):
        return self.adapter


class FailedAdapter(Adapter):
    async def get_transaction_receipt(self, _tx_hash):
        return {"status": "0x0"}


@pytest.mark.asyncio
async def test_transfer_prepare_returns_unsigned_transaction():
    graph = build_graph(model=Model(), chains={"BASE": Adapter()})
    store = InMemorySessionStore()
    session = SwapSessionRecord(session_id="s1", user_id="alice", thread_id="t1")
    await store.save(session)
    app = create_app(graph=graph, chain_registry=Registry(Adapter()), store=store)
    payload = {
        "user_id": "alice",
        "chain": "BASE",
        "sender": "0x" + "1" * 40,
        "recipient": "0x" + "2" * 40,
        "amount": "1",
        "amount_raw": "100",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/transfer/s1/prepare", json=payload)
    assert response.status_code == 200
    assert response.json()["pending_transaction"]["value"] == "100"


@pytest.mark.asyncio
async def test_transfer_broadcast_persists_hash_and_is_idempotent():
    tx_hash = "0x" + "a" * 64
    transaction = UnsignedTransaction(
        chain="BASE",
        chain_id=8453,
        to="0x" + "2" * 40,
        data="0x",
        value="100",
        display={"from": "0x" + "1" * 40},
    )
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="transfer-broadcast",
            user_id="alice",
            thread_id="transfer-thread",
            status="transfer_ready",
            stage="transfer_ready",
            pending_transaction=transaction,
        )
    )
    app = create_app(chain_registry=Registry(Adapter()), store=store)
    payload = {"user_id": "alice", "chain": "BASE", "tx_hash": tx_hash}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/v1/transfer/transfer-broadcast/broadcast", json=payload
        )
        second = await client.post(
            "/v1/transfer/transfer-broadcast/broadcast", json=payload
        )
        wrong_chain = await client.post(
            "/v1/transfer/transfer-broadcast/broadcast",
            json={**payload, "chain": "BSC"},
        )

    assert first.status_code == 200
    assert first.json()["broadcast_tx_hash"] == tx_hash
    assert first.json()["broadcast_status"] == "broadcast_pending"
    assert second.status_code == 200
    assert second.json()["broadcast_tx_hash"] == tx_hash
    assert wrong_chain.status_code == 409
    persisted = await store.get("transfer-broadcast")
    assert persisted is not None
    assert persisted.broadcast_tx_hash == tx_hash
    assert persisted.broadcast_status == "broadcast_pending"


@pytest.mark.asyncio
async def test_transfer_broadcast_persists_failed_receipt_for_status_queries():
    tx_hash = "0x" + "b" * 64
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="failed-transfer",
            user_id="alice",
            thread_id="failed-transfer-thread",
            status="transfer_ready",
            stage="transfer_ready",
            pending_transaction=UnsignedTransaction(
                chain="BASE",
                chain_id=8453,
                to="0x" + "2" * 40,
                data="0x",
                value="100",
            ),
        )
    )
    app = create_app(chain_registry=Registry(FailedAdapter()), store=store)
    payload = {"user_id": "alice", "chain": "BASE", "tx_hash": tx_hash}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/v1/transfer/failed-transfer/broadcast", json=payload)
        second = await client.post("/v1/transfer/failed-transfer/broadcast", json=payload)

    assert first.status_code == 200
    assert first.json()["broadcast_status"] == "failed"
    assert second.status_code == 200
    persisted = await store.get("failed-transfer")
    assert persisted is not None
    assert persisted.broadcast_tx_hash == tx_hash
    assert persisted.broadcast_status == "failed"
