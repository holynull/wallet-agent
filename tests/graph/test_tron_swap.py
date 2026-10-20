"""TRON swap preparation: provider calldata is rebuilt as a node-built protobuf."""

from decimal import Decimal

import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.chains.registry import ChainAdapterRegistry
from wallet_agent.chains.tron import TronChainAdapter
from wallet_agent.domain.models import (
    Asset,
    DepositOrder,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    UnsignedTransaction,
)
from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import (
    _resolve_swap_transaction,
    _transaction_preflight,
)
from wallet_agent.persistence import InMemorySessionStore

OWNER = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
CONTRACT = "TFutBMkB47rYtn5EwECfnfDgDmkUP361Sy"
USDT = Asset(chain="TRON", symbol="USDT", decimals=6, address=OWNER)


class TronNodeFake:
    """Minimal TRON node stub: balances, fee params, and triggersmartcontract."""

    def __init__(self, balance: int = 5_000_000_000) -> None:
        self.balance = balance
        self.posts: list[tuple[str, dict]] = []

    async def get(self, path, **kwargs):
        if path.endswith("getchainparameters"):
            return {"chainParameter": [{"key": "getTransactionFee", "value": 1000}]}
        if path.startswith("/v1/accounts/"):
            return {"data": [{"balance": self.balance}]}
        return {}

    async def post(self, path, **kwargs):
        payload = kwargs.get("json") or {}
        self.posts.append((path, payload))
        if path.endswith("/createtransaction"):
            return {"txID": "aa" * 32, "raw_data_hex": "0a02dead"}
        if path.endswith("/triggersmartcontract"):
            return {
                "result": {"result": True},
                "transaction": {"txID": "cc" * 32, "raw_data_hex": "0a02cafe"},
            }
        if path.endswith("/gettransactionbyid"):
            return {"ret": [{"contractRet": "SUCCESS"}]}
        return {}


def make_quote() -> NormalizedQuote:
    return NormalizedQuote(
        provider="bridgers",
        source_asset=Asset(chain="TRON", symbol="TRX", decimals=6),
        destination_asset=Asset(chain="ETH", symbol="USDT", decimals=6, address="0x" + "2" * 40),
        input_amount=Decimal("100"),
        input_amount_raw="100000000",
        expected_output=Decimal("40"),
        expected_output_raw="40000000",
        provider_reference="bridgers-tron-ref",
    )


def make_provider_tx(**display) -> UnsignedTransaction:
    """EVM-shaped payload Bridgers returns for a TRON swap (see provider docs)."""
    calldata = (
        "0xa9059cbb"
        + "0" * 24
        + "412fcb6e2f0a4b8f1c9ecb1f8d1a0c3b8f2e7d55"
        + "0" * 58
        + "16e360"
    )
    return UnsignedTransaction(
        chain="TRON",
        to=CONTRACT,
        data=calldata,
        value="0x0",
        provider="bridgers",
        provider_reference="bridgers-tron-ref",
        display=display,
    )


@pytest.mark.asyncio
async def test_resolve_swap_transaction_rebuilds_tron_calldata_as_protobuf():
    adapter = TronChainAdapter(TronNodeFake())

    prepared = await _resolve_swap_transaction(
        make_provider_tx(input_amount="100", minimum_output="39.8"),
        quote=make_quote(),
        adapter=adapter,
        sender=OWNER,
        chain="TRON",
    )

    assert isinstance(prepared, UnsignedTransaction)
    assert prepared.data == "0a02cafe"  # node-built raw_data_hex, not calldata
    assert prepared.to == CONTRACT
    assert prepared.value == "0"
    assert prepared.provider == "bridgers"
    assert prepared.provider_reference == "bridgers-tron-ref"
    assert prepared.display["action"] == "contract_call"
    assert prepared.display["provider_order_id"] == "bridgers-tron-ref"
    assert prepared.display["input_amount"] == "100"
    path, payload = adapter.transport.posts[0]
    assert path.endswith("/wallet/triggersmartcontract")
    assert payload["owner_address"] == OWNER
    assert payload["contract_address"] == CONTRACT
    assert payload["data"].startswith("a9059cbb")
    assert payload["call_value"] == 0


@pytest.mark.asyncio
async def test_resolve_swap_transaction_leaves_chain_ready_tron_payload_untouched():
    adapter = TronChainAdapter(TronNodeFake())
    chain_ready = UnsignedTransaction(
        chain="TRON", to=CONTRACT, data="0a02cafe", value="0", display={"action": "contract_call"}
    )

    resolved = await _resolve_swap_transaction(
        chain_ready, quote=make_quote(), adapter=adapter, sender=OWNER, chain="TRON"
    )

    assert resolved is chain_ready
    assert adapter.transport.posts == []


@pytest.mark.asyncio
async def test_resolve_swap_transaction_builds_tron_deposit_order_as_trc20_transfer():
    adapter = TronChainAdapter(TronNodeFake())
    order = DepositOrder(
        provider="omnibridge",
        provider_order_id="ob-tron",
        deposit_address=CONTRACT,
        source_asset=USDT,
        destination_asset=Asset(chain="ETH", symbol="USDT", decimals=6, address="0x" + "2" * 40),
        input_amount=Decimal("1"),
        input_amount_raw="1000000",
        provider_reference="ob-tron-ref",
    )

    resolved = await _resolve_swap_transaction(
        order, quote=make_quote(), adapter=adapter, sender=OWNER, chain="TRON"
    )

    assert isinstance(resolved, UnsignedTransaction)
    path, payload = adapter.transport.posts[0]
    assert path.endswith("/wallet/triggersmartcontract")
    assert payload["data"].startswith("a9059cbb")
    assert resolved.display["action"] == "provider_deposit"
    assert resolved.display["deposit_address"] == CONTRACT


@pytest.mark.asyncio
async def test_preflight_skips_okx_evm_checks_for_tron():
    class RecordingOkx:
        def __init__(self):
            self.calls = 0

        async def estimate_gas_limit(self, transaction):  # pragma: no cover - must not run
            self.calls += 1
            raise AssertionError("OKX gas estimation must not run for TRON")

        async def simulate_transaction(self, transaction):  # pragma: no cover - must not run
            self.calls += 1
            raise AssertionError("OKX simulation must not run for TRON")

    adapter = TronChainAdapter(TronNodeFake())
    okx = RecordingOkx()
    tx = await adapter.build_native_transfer(
        from_address=OWNER, to_address=CONTRACT, amount_raw="120000"
    )

    result = await _transaction_preflight(
        adapter=adapter,
        chain="TRON",
        sender=OWNER,
        recipient=CONTRACT,
        amount_raw="120000",
        token=None,
        transaction=tx,
        wallet_context={"address": OWNER, "chain": "TRON"},
        wallet_provider=okx,
    )

    assert result["ok"] is True
    assert okx.calls == 0
    assert result["gas_sources"]["okx"] == "unsupported"
    assert result["gas_sources"]["simulation"] == "unsupported"


class FakeModel:
    async def ainvoke(self, _value):
        return {"intent": "swap_quote"}


class BridgersTronFake:
    """Provider that quotes TRX -> ETH and returns an EVM-shaped swap payload."""

    provider_name = "bridgers"

    async def quote(self, request):
        return make_quote()

    async def prepare(self, quote):
        return make_provider_tx(input_amount="100", expected_output="40", minimum_output="39.8")

    async def register_broadcast(self, provider_reference, tx_hash, **kwargs):
        return ProviderOrder(
            provider="bridgers",
            provider_order_id="order-tron",
            provider_reference=provider_reference,
            tx_hash=tx_hash,
        )


@pytest.mark.asyncio
async def test_tron_swap_prepare_returns_raw_data_hex_for_the_wallet():
    tron = TronChainAdapter(TronNodeFake())
    provider = BridgersTronFake()
    graph = build_graph(model=FakeModel(), providers=[provider], chains={"TRON": tron})
    app = create_app(
        graph=graph,
        providers={"bridgers": provider},
        chain_registry=ChainAdapterRegistry({"TRON": tron}),
        store=InMemorySessionStore(),
    )
    request = SwapQuoteRequest(
        source_asset=Asset(chain="TRON", symbol="TRX", decimals=6),
        destination_asset=Asset(chain="ETH", symbol="USDT", decimals=6, address="0x" + "2" * 40),
        input_amount=Decimal("100"),
        input_amount_raw="100000000",
        sender_address=OWNER,
        recipient_address=OWNER,
    )
    payload = {
        "conversation_id": "tron-swap",
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
    assert transaction["data"] == "0a02cafe"
    assert transaction["to"] == CONTRACT
    assert transaction["display"]["action"] == "contract_call"
    assert transaction["display"]["provider"] == "bridgers"
