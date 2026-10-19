"""Bitcoin swap preparation: UTXO sources pay the provider's deposit address."""

import base64
from decimal import Decimal

import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.chains.btc import PSBT_MAGIC, BitcoinChainAdapter
from wallet_agent.chains.registry import ChainAdapterRegistry, build_default_registry
from wallet_agent.domain.chains import Capability
from wallet_agent.domain.models import (
    Asset,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    UnsignedTransaction,
)
from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import (
    _resolve_swap_transaction,
    _transaction_preflight,
    _utxo_deposit_order,
)
from wallet_agent.persistence import InMemorySessionStore

P2WPKH = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"
P2TR = "bc1p47u7v2k5cpfqt0gxftkat3ade7ucs588g8lr0znzr9le06taty6q6x9gut"

UTXOS = [{"txid": "aa" * 32, "vout": 0, "value": 200_000, "status": {"confirmed": True}}]


class EsploraFake:
    """Minimal Esplora stub: UTXO set, mempool fee target, and address stats."""

    def __init__(self, utxos=None, balance=200_000):
        self.utxos = UTXOS if utxos is None else utxos
        self.balance = balance

    async def get(self, path, **_kwargs):
        if path.endswith("/utxo"):
            return self.utxos
        if path.endswith("/fee-estimates"):
            return {"6": 3.0}
        if path.endswith("/hex"):
            return "0100000001" + "cc" * 32 + "0000000000ffffffff01"
        if path.endswith("/status"):
            return {"confirmed": False}
        if path.endswith("/txs"):
            return []
        if "/address/" in path:
            return {"chain_stats": {"funded_txo_sum": self.balance, "spent_txo_sum": 0}}
        return {}


def make_quote() -> NormalizedQuote:
    return NormalizedQuote(
        provider="bridgers",
        source_asset=Asset(chain="BTC", symbol="BTC", decimals=8),
        destination_asset=Asset(chain="ETH", symbol="USDT", decimals=6, address="0x" + "2" * 40),
        input_amount=Decimal("0.0012"),
        input_amount_raw="120000",
        expected_output=Decimal("40"),
        expected_output_raw="40000000",
        provider_reference="bridgers-ref",
    )


def make_provider_tx(**display) -> UnsignedTransaction:
    return UnsignedTransaction(
        chain="BTC",
        to=P2TR,
        data="0x",
        value="0x1d4c0",
        provider="bridgers",
        provider_reference="bridgers-ref",
        display=display,
    )


def test_default_registry_registers_a_capable_bitcoin_adapter():
    registry = build_default_registry(rpc_urls={"BTC": ["https://blockstream.info"]})

    adapter = registry.get("BTC")

    assert isinstance(adapter, BitcoinChainAdapter)
    assert adapter.capabilities.available == frozenset(Capability)
    assert adapter.transaction_model == "utxo"


def test_default_registry_normalizes_esplora_api_bases():
    for endpoint in ("https://blockstream.info", "https://blockstream.info/api"):
        adapter = build_default_registry(rpc_urls={"BTC": [endpoint]}).get("BTC")
        assert adapter.transport.urls == ["https://blockstream.info"]
        assert adapter.base_path == "/api"


def test_utxo_deposit_order_maps_evm_shaped_payload_onto_deposit_transfer():
    order = _utxo_deposit_order(make_quote(), make_provider_tx())

    assert order is not None
    assert order.deposit_address == P2TR
    assert order.input_amount_raw == "120000"
    assert order.provider_reference == "bridgers-ref"
    assert order.source_asset.chain == "BTC"


def test_utxo_deposit_order_ignores_already_built_transactions():
    prepared = make_provider_tx(action="native_transfer")
    assert _utxo_deposit_order(make_quote(), prepared) is None


@pytest.mark.asyncio
async def test_resolve_swap_transaction_rebuilds_btc_deposit_as_psbt():
    adapter = BitcoinChainAdapter(EsploraFake())

    prepared = await _resolve_swap_transaction(
        make_provider_tx(deposit_address=P2TR),
        quote=make_quote(),
        adapter=adapter,
        sender=P2WPKH,
        chain="BTC",
    )

    assert isinstance(prepared, UnsignedTransaction)
    assert prepared.to == P2TR
    assert prepared.value == "120000"
    assert prepared.data != "0x"
    assert base64.b64decode(prepared.data).startswith(PSBT_MAGIC)
    assert prepared.display["action"] == "provider_deposit"
    assert prepared.display["deposit_address"] == P2TR
    assert prepared.display["provider_order_id"] == "bridgers-ref"
    assert prepared.display["unsigned_tx_hex"].startswith("0200000001")
    assert prepared.provider == "bridgers"


@pytest.mark.asyncio
async def test_resolve_swap_transaction_leaves_evm_payloads_untouched():
    class EvmAdapter:
        transaction_model = "evm"

    provider_tx = UnsignedTransaction(chain="ETH", to="0xrouter", data="0xdeadbeef", value="0x0")

    resolved = await _resolve_swap_transaction(
        provider_tx, quote=None, adapter=EvmAdapter(), sender="0x" + "1" * 40, chain="ETH"
    )

    assert resolved is provider_tx


@pytest.mark.asyncio
async def test_preflight_skips_okx_evm_checks_for_bitcoin():
    class RecordingOkx:
        def __init__(self):
            self.calls = 0

        async def estimate_gas_limit(self, transaction):  # pragma: no cover - must not run
            self.calls += 1
            raise AssertionError("OKX gas estimation must not run for Bitcoin")

        async def simulate_transaction(self, transaction):  # pragma: no cover - must not run
            self.calls += 1
            raise AssertionError("OKX simulation must not run for Bitcoin")

    adapter = BitcoinChainAdapter(EsploraFake())
    okx = RecordingOkx()
    tx = await adapter.build_native_transfer(
        from_address=P2WPKH, to_address=P2TR, amount_raw="120000"
    )

    result = await _transaction_preflight(
        adapter=adapter,
        chain="BTC",
        sender=P2WPKH,
        recipient=P2TR,
        amount_raw="120000",
        token=None,
        transaction=tx,
        wallet_context={"address": P2WPKH, "chain": "BTC"},
        wallet_provider=okx,
    )

    assert result["ok"] is True
    assert okx.calls == 0
    assert result["gas_sources"]["okx"] == "unsupported"
    assert result["gas_sources"]["simulation"] == "unsupported"


@pytest.mark.asyncio
async def test_preflight_accepts_foreign_family_connected_account_for_bitcoin():
    """An EVM-connected browser wallet cannot sign BTC; the App's BTC account does."""
    adapter = BitcoinChainAdapter(EsploraFake())
    tx = await adapter.build_native_transfer(
        from_address=P2WPKH, to_address=P2TR, amount_raw="120000"
    )
    evm_connected = "0x" + "3f847a21bcfbba535a5e2a1611bb4d545541d2a5"

    result = await _transaction_preflight(
        adapter=adapter,
        chain="BTC",
        sender=P2WPKH,
        recipient=P2TR,
        amount_raw="120000",
        token=None,
        transaction=tx,
        wallet_context={"address": evm_connected, "chain": "ETH"},
    )

    check = next(item for item in result["checks"] if item["name"] == "wallet_account")
    assert check["status"] == "warning"
    assert check["code"] == "WALLET_ACCOUNT_EXTERNAL_SIGNER"
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_preflight_reports_insufficient_bitcoin_balance():
    adapter = BitcoinChainAdapter(EsploraFake(balance=0))
    tx = UnsignedTransaction(chain="BTC", to=P2TR, data="psbt", value="120000")

    result = await _transaction_preflight(
        adapter=adapter,
        chain="BTC",
        sender=P2WPKH,
        recipient=P2TR,
        amount_raw="120000",
        token=None,
        transaction=tx,
        wallet_context={"address": P2WPKH, "chain": "BTC"},
        wallet_provider=None,
    )

    assert result["ok"] is False
    assert any(item.get("code") == "INSUFFICIENT_BALANCE" for item in result["checks"])


class FakeModel:
    async def ainvoke(self, _value):
        return {"intent": "swap_quote"}


class BridgersFake:
    """Provider that quotes BTC -> ETH and returns an EVM-shaped deposit payload."""

    provider_name = "bridgers"
    memo = "xpt:to:USDT(ERC20):0x" + "2" * 40

    async def quote(self, request):
        return NormalizedQuote(
            provider="bridgers",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal("40"),
            expected_output_raw="40000000",
            provider_reference="bridgers-ref",
        )

    async def prepare(self, quote):
        return UnsignedTransaction(
            chain="BTC",
            to=P2TR,
            data="0x",
            value="0x1d4c0",
            provider="bridgers",
            provider_reference=quote.provider_reference,
            display={"deposit_address": P2TR, "memo": self.memo},
        )

    async def register_broadcast(self, provider_reference, tx_hash, **kwargs):
        return ProviderOrder(
            provider="bridgers",
            provider_order_id="order-btc",
            provider_reference=provider_reference,
            tx_hash=tx_hash,
        )


@pytest.mark.asyncio
async def test_btc_swap_prepare_returns_a_psbt_paying_the_deposit_address():
    btc = BitcoinChainAdapter(EsploraFake())
    provider = BridgersFake()
    graph = build_graph(model=FakeModel(), providers=[provider], chains={"BTC": btc})
    app = create_app(
        graph=graph,
        providers={"bridgers": provider},
        chain_registry=ChainAdapterRegistry({"BTC": btc}),
        store=InMemorySessionStore(),
    )
    request = SwapQuoteRequest(
        source_asset=Asset(chain="BTC", symbol="BTC", decimals=8),
        destination_asset=Asset(chain="ETH", symbol="USDT", decimals=6, address="0x" + "2" * 40),
        input_amount=Decimal("0.0012"),
        input_amount_raw="120000",
        sender_address=P2WPKH,
        recipient_address=P2WPKH,
    )
    payload = {
        "conversation_id": "btc-swap",
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
        session = (
            await client.get(f"/v1/swap/{session_id}", params={"user_id": "alice"})
        ).json()

    transaction = prepared.json()["pending_transaction"]
    # The confirmation the user approves must name the address the order pays out to.
    assert session["confirmation_state"]["summary"]["recipient_address"] == P2WPKH
    assert transaction["chain"] == "BTC"
    assert transaction["to"] == P2TR
    assert transaction["value"] == "120000"
    assert transaction["display"]["action"] == "provider_deposit"
    assert transaction["display"]["deposit_address"] == P2TR
    # The provider's routing hint must survive the rebuild into a PSBT.
    assert transaction["display"]["memo"] == BridgersFake.memo
    assert transaction["display"]["unsigned_tx_hex"].startswith("0200000001")
    assert base64.b64decode(transaction["data"]).startswith(PSBT_MAGIC)
