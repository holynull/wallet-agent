"""Cross-chain swap preflight: a destination-chain payout must not fail source checks.

Regression: an ETH-source swap (USDT -> BTC) reported TRANSACTION_PREFLIGHT_FAILED
with INVALID_ADDRESS because the preflight recipient was the BTC payout address
(``swap_request.recipient_address``). Bridgers' ``prepare`` returns a transaction
whose display has no ``deposit_address``, so the old code fell back to the
destination payout and the ETH adapter rejected it. The preflight must only ever
validate a source-chain address (the deposit address / transaction ``to``), and a
foreign-chain recipient must stay a non-blocking warning.
"""

from decimal import Decimal

import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.chains.evm import EVMChainAdapter
from wallet_agent.chains.registry import ChainAdapterRegistry
from wallet_agent.domain.models import (
    Asset,
    NormalizedQuote,
    SwapQuoteRequest,
    UnsignedTransaction,
)
from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import (
    _swap_preflight_recipient,
    _transaction_preflight,
)
from wallet_agent.persistence import InMemorySessionStore

WALLET = "0x" + "1" * 40
ETH_DEPOSIT = "0x" + "2" * 40
TOKEN = "0x" + "4" * 40
BTC_PAYOUT = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"

USDT = Asset(chain="ETH", chain_id=1, symbol="USDT", decimals=6, address=TOKEN)
BTC = Asset(chain="BTC", symbol="BTC", decimals=8)


class RpcFake:
    async def request(self, method, params):
        values = {
            "eth_getBalance": "0x16345785d8a0000",  # 0.1 ETH
            "eth_call": "0x0f4240",  # 1_000_000 (USDT with 6 decimals)
            "eth_estimateGas": "0x5208",
            "eth_gasPrice": "0x3b9aca00",
            "eth_getTransactionReceipt": {"status": "0x1", "blockNumber": "0x10"},
        }
        if method not in values:
            raise AssertionError(f"unexpected RPC method {method!r}")
        return values[method]


def _bridgers_eth_tx(**display) -> UnsignedTransaction:
    """Mirror Bridgers' EVM-shaped prepare: no ``deposit_address`` in display."""
    return UnsignedTransaction(
        chain="ETH",
        to=ETH_DEPOSIT,
        data="0xa9059cbb" + "0" * 24 + ETH_DEPOSIT[2:] + "0" * 63 + "1",
        value="0x0",
        provider="bridgers",
        provider_reference="bridgers-ref",
        display={
            "input_amount": "1",
            "expected_output": "0.00002",
            "minimum_output": "0",
            **display,
        },
    )


def test_swap_preflight_recipient_never_uses_destination_payout():
    # Cross-chain: display has no deposit_address; transaction.to is the ETH
    # deposit contract and must win over the BTC payout.
    tx = _bridgers_eth_tx()
    request = {"recipient_address": BTC_PAYOUT}
    assert _swap_preflight_recipient(tx, request, "ETH") == ETH_DEPOSIT

    # A display deposit_address (omnibridge-style) is preferred even over ``to``.
    tx = _bridgers_eth_tx(deposit_address=TOKEN)
    assert _swap_preflight_recipient(tx, request, "ETH") == TOKEN

    # Same-chain swap with no deposit concept: the transaction ``to`` (a valid
    # source address) is preferred over the recipient payout.
    tx = UnsignedTransaction(chain="ETH", to=TOKEN, data="0x", value="0")
    request = {"recipient_address": WALLET}
    assert _swap_preflight_recipient(tx, request, "ETH") == TOKEN

    # Only when nothing source-chain is present does a valid source payout win.
    tx = UnsignedTransaction(chain="ETH", to="", data="0x", value="0")
    assert _swap_preflight_recipient(tx, request, "ETH") == WALLET

    # Only foreign addresses present -> nothing source-chain to validate.
    tx = UnsignedTransaction(chain="ETH", to="", data="0x", value="0")
    assert _swap_preflight_recipient(tx, {"recipient_address": BTC_PAYOUT}, "ETH") == ""


@pytest.mark.asyncio
async def test_preflight_foreign_chain_recipient_is_non_blocking():
    """A BTC payout on an ETH preflight warns instead of failing addresses."""
    adapter = EVMChainAdapter(RpcFake(), chain="ETH", chain_id=1)
    tx = _bridgers_eth_tx()

    result = await _transaction_preflight(
        adapter=adapter,
        chain="ETH",
        sender=WALLET,
        recipient=BTC_PAYOUT,
        amount_raw="1000000",
        token=USDT,
        transaction=tx,
        wallet_context={"address": WALLET, "chain": "ETH"},
    )

    assert result["ok"] is True
    check = next(item for item in result["checks"] if item["name"] == "addresses")
    assert check["status"] == "warning"
    assert check["code"] == "CHECK_PARTIAL"
    assert check["details"]["cross_chain"] is True


class FakeModel:
    async def ainvoke(self, _value):
        return {"intent": "swap_quote"}


class BridgersEthFake:
    """Provider that quotes USDT(ETH) -> BTC and returns an EVM-shaped payload."""

    provider_name = "bridgers"

    async def quote(self, request):
        return NormalizedQuote(
            provider="bridgers",
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=Decimal("0.00002"),
            expected_output_raw="2000",
            provider_reference="bridgers-ref",
        )

    async def prepare(self, quote):
        return _bridgers_eth_tx()

    async def register_broadcast(self, provider_reference, tx_hash, **kwargs):
        return None


@pytest.mark.asyncio
async def test_eth_source_swap_with_btc_payout_prepares_successfully():
    """The exact reported flow: USDT(ETH) -> BTC with a BTC payout address."""
    adapter = EVMChainAdapter(RpcFake(), chain="ETH", chain_id=1)
    provider = BridgersEthFake()
    graph = build_graph(model=FakeModel(), providers=[provider], chains={"ETH": adapter})
    app = create_app(
        graph=graph,
        providers={"bridgers": provider},
        chain_registry=ChainAdapterRegistry({"ETH": adapter}),
        store=InMemorySessionStore(),
    )
    request = SwapQuoteRequest(
        source_asset=USDT,
        destination_asset=BTC,
        input_amount=Decimal("1"),
        input_amount_raw="1000000",
        sender_address=WALLET,
        recipient_address=BTC_PAYOUT,
    )
    payload = {
        "conversation_id": "eth-btc-swap",
        "user_id": "alice",
        "message": "swap",
        "metadata": {"swap_request": request.model_dump(mode="json")},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        turn = await client.post("/v1/agent/turn", json=payload)
        body = turn.json()
        await app.state.runs[body["run_id"]]["task"]
        session_id = body["session_id"]
        prepared = await client.post(
            f"/v1/swap/{session_id}/confirm",
            json={"user_id": "alice", "approved": True},
        )

    prepared_body = prepared.json()
    assert prepared_body["pending_transaction"]["chain"] == "ETH"
    assert prepared_body["pending_transaction"]["to"] == ETH_DEPOSIT
    preflight = prepared_body.get("preflight") or {}
    assert preflight.get("ok") is True
    addresses = next(
        item for item in (preflight.get("checks") or []) if item.get("name") == "addresses"
    )
    assert addresses["status"] != "failed"
