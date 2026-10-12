"""The session index must be projected before a run's terminal SSE frame.

An App that reads or selects a quote the moment the run ends otherwise sees a
session that has not been updated yet, and gets a 404 from ``/select-quote``.
"""

from decimal import Decimal

import httpx
import pytest

from wallet_agent.api import create_app
from wallet_agent.chains.btc import BitcoinChainAdapter
from wallet_agent.chains.registry import ChainAdapterRegistry
from wallet_agent.domain.models import NormalizedQuote
from wallet_agent.graph.build import build_graph
from wallet_agent.persistence import InMemorySessionStore
from wallet_agent.persistence.runtime import InMemoryRunStore

BTC_SENDER = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"
EVM_RECIPIENT = "0x" + "1" * 40
USDT_ETH = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USER = "alice"
TERMINAL_EVENTS = {"complete", "error", "action_required"}


class EsploraFake:
    """The quote step never reads the chain, but the registry needs an adapter."""

    async def get(self, _path, **_kwargs):
        return {}


class QuoteModel:
    async def ainvoke(self, _value):
        return {"intent": "swap_quote"}


class StaticProvider:
    """Minimal provider: two of these make the quote step stop for a choice."""

    def __init__(self, name: str, rate: str) -> None:
        self.provider_name = name
        self.rate = Decimal(rate)

    async def quote(self, request):
        return NormalizedQuote(
            provider=self.provider_name,
            source_asset=request.source_asset,
            destination_asset=request.destination_asset,
            input_amount=request.input_amount,
            input_amount_raw=request.input_amount_raw,
            expected_output=request.input_amount * self.rate,
            expected_output_raw=str(int(request.input_amount * self.rate * 10**6)),
            minimum_output=request.input_amount * self.rate,
            minimum_output_raw=str(int(request.input_amount * self.rate * 10**6)),
            provider_reference=f"{self.provider_name}-ref",
        )


class RecordingRunStore(InMemoryRunStore):
    def __init__(self, order: list[tuple[str, object]]) -> None:
        super().__init__()
        self.order = order

    async def append(self, run_id: str, event):
        if event.get("event") in TERMINAL_EVENTS:
            self.order.append(("terminal_frame", event.get("event")))
        return await super().append(run_id, event)


class RecordingSessionStore(InMemorySessionStore):
    def __init__(self, order: list[tuple[str, object]]) -> None:
        super().__init__()
        self.order = order

    async def update(self, session_id: str, **changes):
        if "quote_candidates" in changes:
            self.order.append(("session_projection", len(changes["quote_candidates"] or [])))
        return await super().update(session_id, **changes)


def swap_payload() -> dict:
    return {
        "conversation_id": "terminal-ordering",
        "user_id": USER,
        "message": "把 0.0002 BTC 换成 Ethereum 上的 USDT",
        "address": BTC_SENDER,
        "chain": "BTC",
        "supported_networks": ["BTC", "ETH"],
        "wallet_accounts": [
            {"chain": "BTC", "address": BTC_SENDER},
            {"chain": "ETH", "address": EVM_RECIPIENT},
        ],
        "metadata": {
            "agent_test_intent": "swap_quote",
            "swap_request": {
                "source_asset": {"chain": "BTC", "symbol": "BTC", "decimals": 8},
                "destination_asset": {
                    "chain": "ETH",
                    "chain_id": 1,
                    "symbol": "USDT",
                    "decimals": 6,
                    "address": USDT_ETH,
                },
                "input_amount": "0.0002",
                "input_amount_raw": "20000",
                "sender_address": BTC_SENDER,
                "recipient_address": EVM_RECIPIENT,
            },
        },
    }


@pytest.mark.asyncio
async def test_session_is_projected_before_the_terminal_stream_frame():
    order: list[tuple[str, object]] = []
    providers = {
        "bridgers": StaticProvider("bridgers", "82000"),
        "omnibridge": StaticProvider("omnibridge", "81800"),
    }
    adapter = BitcoinChainAdapter(EsploraFake())
    graph = build_graph(model=QuoteModel(), providers=providers, chains={"BTC": adapter})
    app = create_app(
        graph=graph,
        providers=providers,
        chain_registry=ChainAdapterRegistry({"BTC": adapter}),
        store=RecordingSessionStore(order),
        run_store=RecordingRunStore(order),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        turn = await client.post("/v1/agent/turn", json=swap_payload())
        body = turn.json()
        session_id = body["session_id"]
        terminal = None
        async with client.stream("GET", f"/v1/agent/stream/{body['run_id']}") as stream:
            async for line in stream.aiter_lines():
                if line.startswith("event: ") and line[len("event: ") :] in TERMINAL_EVENTS:
                    terminal = line[len("event: ") :]
                    break
        # No sleep: the session must already carry the quotes the client was
        # just told about.
        session = (
            await client.get(f"/v1/swap/{session_id}", params={"user_id": USER})
        ).json()

    assert terminal == "complete"
    kinds = [item[0] for item in order]
    terminal_index = kinds.index("terminal_frame")
    projection_index = kinds.index("session_projection")
    projected = next(
        item for item in order if item[0] == "session_projection" and item[1]
    )
    assert projection_index < terminal_index
    assert projected[1] == 2
    assert session["stage"] == "selecting_quote"
    assert len(session["quote_candidates"]) == 2
