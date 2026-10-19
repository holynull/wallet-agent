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
    body = response.json()
    assert body["pending_transaction"]["value"] == "100"
    # The unsigned-transaction card shows what moves: the asset identity (with
    # its icon) rides next to the payload the wallet signs, never inside it, and
    # the REST session projection carries the same row as the SSE frame.
    assert body["asset"]["symbol"] == "ETH"
    assert body["asset"]["logo_url"].endswith("/blockchains/base/info/logo.png")
    assert body["chain_logo_url"].endswith("/blockchains/base/info/logo.png")


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


BTC_SENDER = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"
BTC_RECIPIENT = "bc1p47u7v2k5cpfqt0gxftkat3ade7ucs588g8lr0znzr9le06taty6q6x9gut"


class EsploraFake:
    async def get(self, path, **_kwargs):
        if path.endswith("/utxo"):
            return [{"txid": "aa" * 32, "vout": 0, "value": 200_000, "status": {}}]
        if path.endswith("/fee-estimates"):
            return {"6": 3.0}
        if path.endswith("/status"):
            return {"confirmed": True}
        if "/address/" in path:
            return {"chain_stats": {"funded_txo_sum": 200_000, "spent_txo_sum": 0}}
        return {}


@pytest.mark.asyncio
async def test_bitcoin_transfer_prepare_returns_a_psbt():
    import base64

    from wallet_agent.chains.btc import PSBT_MAGIC, BitcoinChainAdapter

    adapter = BitcoinChainAdapter(EsploraFake())
    graph = build_graph(model=Model(), chains={"BTC": adapter})
    store = InMemorySessionStore()
    await store.save(SwapSessionRecord(session_id="btc-1", user_id="alice", thread_id="btc-t"))
    app = create_app(graph=graph, chain_registry=Registry(adapter), store=store)
    payload = {
        "user_id": "alice",
        "chain": "BTC",
        "sender": BTC_SENDER,
        "recipient": BTC_RECIPIENT,
        "amount": "0.0012",
        "amount_raw": "120000",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/transfer/btc-1/prepare", json=payload)

    assert response.status_code == 200
    transaction = response.json()["pending_transaction"]
    assert transaction["chain"] == "BTC"
    assert transaction["to"] == BTC_RECIPIENT
    assert transaction["value"] == "120000"
    assert base64.b64decode(transaction["data"]).startswith(PSBT_MAGIC)
    assert transaction["display"]["action"] == "native_transfer"
    assert transaction["display"]["fee_raw"] == "459"


@pytest.mark.asyncio
async def test_bitcoin_transfer_broadcast_accepts_a_bitcoin_txid():
    """The app signs the PSBT itself and reports the txid back."""
    from wallet_agent.chains.btc import BitcoinChainAdapter

    adapter = BitcoinChainAdapter(EsploraFake())
    graph = build_graph(model=Model(), chains={"BTC": adapter})
    store = InMemorySessionStore()
    await store.save(SwapSessionRecord(session_id="btc-2", user_id="alice", thread_id="btc-t2"))
    app = create_app(graph=graph, chain_registry=Registry(adapter), store=store)
    txid = "b" * 64
    prepare_payload = {
        "user_id": "alice",
        "chain": "BTC",
        "sender": BTC_SENDER,
        "recipient": BTC_RECIPIENT,
        "amount": "0.0012",
        "amount_raw": "120000",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        prepared = await client.post("/v1/transfer/btc-2/prepare", json=prepare_payload)
        assert prepared.status_code == 200
        response = await client.post(
            "/v1/transfer/btc-2/broadcast",
            json={"user_id": "alice", "chain": "BTC", "tx_hash": txid},
        )

    assert response.status_code == 200
    assert response.json()["broadcast_status"] == "confirmed"
    persisted = await store.get("btc-2")
    assert persisted is not None
    assert persisted.broadcast_tx_hash == txid


def test_assistant_content_explains_bitcoin_psbt_signing():
    from wallet_agent.api.app import _assistant_response_content

    psbt = {"chain": "BTC", "data": "cHNidP8", "display": {"format": "psbt"}}

    assert _assistant_response_content(
        {"kind": "transfer_prepare", "pending_transaction": psbt}
    ) == "Bitcoin 转账交易已准备好，请把 PSBT 导入比特币钱包签名并广播。"
    assert _assistant_response_content({"kind": "swap_prepare", "transaction": psbt}) == (
        "Bitcoin 兑换交易已准备好，请把 PSBT 导入比特币钱包签名并广播。"
    )
    # EVM and unknown payloads keep the original wording.
    assert _assistant_response_content({"kind": "transfer_prepare"}) == (
        "转账交易已准备好，请在钱包中确认并广播。"
    )
    assert _assistant_response_content(
        {"kind": "transfer_prepare", "pending_transaction": {"chain": "BASE", "data": "0x"}}
    ) == "转账交易已准备好，请在钱包中确认并广播。"
