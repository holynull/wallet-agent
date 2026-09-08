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
