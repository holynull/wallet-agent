"""Bitcoin transfers spend every address the App declared, not just the first."""

import pytest

from wallet_agent.chains.btc import BitcoinChainAdapter
from wallet_agent.graph.build import build_graph
from wallet_agent.graph.nodes import _utxo_source_addresses

P2WPKH = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"
P2TR = "bc1p47u7v2k5cpfqt0gxftkat3ade7ucs588g8lr0znzr9le06taty6q6x9gut"
P2SH = "3FGALr2UNebKnBTErhePUk3CuzmDd9FJux"
EVM_ADDRESS = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"


class FakeModel:
    async def ainvoke(self, value):
        return {"intent": value.get("intent", "clarification")}


class PerAddressEsplora:
    """Esplora stub that answers a distinct UTXO set and balance per address."""

    def __init__(self, utxos_by_address, balances_by_address=None):
        self.utxos_by_address = utxos_by_address
        self.balances_by_address = balances_by_address or {}
        self.paths: list[str] = []

    async def get(self, path, **_kwargs):
        self.paths.append(path)
        if path.endswith("/utxo"):
            address = path.split("/address/")[1].removesuffix("/utxo")
            return self.utxos_by_address.get(address, [])
        if path.endswith("/fee-estimates"):
            return {"6": 3.0}
        if path.endswith("/hex"):
            return "0100000001" + "cc" * 32 + "0000000000ffffffff01"
        if "/address/" in path:
            address = path.split("/address/")[1]
            sats = self.balances_by_address.get(address, 0)
            return {"chain_stats": {"funded_txo_sum": sats, "spent_txo_sum": 0}}
        return {}


def _wallet_context(accounts):
    return {
        "address": EVM_ADDRESS,
        "chain": "ETH",
        "wallet_accounts": [{"chain": "BTC", "address": item} for item in accounts],
    }


def test_utxo_source_addresses_orders_and_dedupes_declared_accounts():
    context = _wallet_context([P2WPKH, P2TR, P2SH, P2WPKH])
    assert _utxo_source_addresses(context, "BTC", P2TR) == [P2TR, P2WPKH, P2SH]
    assert _utxo_source_addresses(context, "BTC", None) == [P2WPKH, P2TR, P2SH]


def test_utxo_source_addresses_ignores_foreign_family_addresses():
    context = _wallet_context([])
    assert _utxo_source_addresses(context, "BTC", EVM_ADDRESS) == []
    assert _utxo_source_addresses(context, "BTC", "not-an-address") == []


@pytest.mark.asyncio
async def test_bitcoin_transfer_pools_the_app_declared_addresses():
    transport = PerAddressEsplora(
        {
            P2WPKH: [{"txid": "aa" * 32, "vout": 0, "value": 1_000}],
            P2TR: [{"txid": "bb" * 32, "vout": 1, "value": 200_000}],
            P2SH: [],
        },
        # The primary address alone cannot cover 120_000 + fee; the wallet can.
        {P2WPKH: 1_000, P2TR: 200_000, P2SH: 0},
    )
    adapter = BitcoinChainAdapter(transport)
    graph = build_graph(model=FakeModel(), chains={"BTC": adapter})

    result = await graph.ainvoke(
        {
            "intent": "transfer",
            "transfer_request": {
                "chain": "BTC",
                "sender": P2WPKH,
                "recipient": P2TR,
                "amount": "0.0012",
                "amount_raw": "120000",
            },
            "wallet_context": _wallet_context([P2WPKH, P2TR, P2SH]),
        },
        config={"configurable": {"thread_id": "btc-transfer-pooled"}},
    )

    assert result["response"]["kind"] == "transfer_prepare"
    transaction = result["response"]["pending_transaction"]
    assert transaction["chain"] == "BTC"
    assert transaction["display"]["input_count"] == "1"
    assert transaction["display"]["from_addresses"] == f"{P2WPKH},{P2TR},{P2SH}"
    # The funds live on the second address, so its UTXOs must be reachable.
    assert f"/api/address/{P2TR}/utxo" in transport.paths
    # The balance check pools those addresses instead of failing on the sender.
    assert result["preflight"]["ok"] is True
    assert not any(
        item.get("code") == "INSUFFICIENT_BALANCE" for item in result["preflight"]["checks"]
    )
    # The preflight reports the fee the builder actually selected (143 vB * 3).
    assert result["preflight"]["fee_estimate"]["amount_raw"] == "429"
