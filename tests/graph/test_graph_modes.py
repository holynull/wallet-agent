import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command


class NoReactModel:
    async def ainvoke(self, _messages):
        return AIMessage(content="legacy")


class UnbindableReactModel:
    supports_react = False

    async def invoke_react(self, _messages, _tools):
        return AIMessage(content="react")


class ReactModel:
    def __init__(self):
        self.calls = []

    async def invoke_react(self, _messages, _tools):
        self.calls.append("react")
        return AIMessage(content="react")


@pytest.mark.asyncio
async def test_strict_graph_rejects_models_without_react_capability():
    from wallet_agent.graph.build import build_graph

    with pytest.raises(RuntimeError, match="required ReAct contract"):
        build_graph(model=NoReactModel(), require_react=True, split_graphs=True)


def test_strict_graph_rejects_models_that_cannot_bind_tools():
    from wallet_agent.graph.build import build_graph

    with pytest.raises(RuntimeError, match="cannot bind wallet tools"):
        build_graph(model=UnbindableReactModel(), require_react=True, split_graphs=True)


@pytest.mark.asyncio
async def test_conversation_graph_is_react_only_and_marks_route():
    from wallet_agent.graph.build import build_graph

    model = ReactModel()
    graph = build_graph(model=model, split_graphs=True, require_react=True)
    result = await graph.ainvoke(
        {"request": {"message": "ETH 的价格"}},
        config={"configurable": {"thread_id": "conversation-only"}},
    )

    assert graph.graph_role == "router"
    assert graph.conversation_graph.graph_role == "conversation"
    assert graph.lifecycle_graph.graph_role == "lifecycle"
    assert {"react_agent", "react_tools"}.isdisjoint(
        graph.lifecycle_graph.get_graph().nodes
    )
    assert result["response"]["kind"] == "react"
    assert result["route_mode"] == "react"
    assert result["route_reason"] == "model_answer"
    assert model.calls == ["react"]


def test_lifecycle_graph_is_marked_separately():
    from wallet_agent.graph.build import build_graph

    graph = build_graph(model=NoReactModel())

    assert graph.graph_role == "lifecycle"


@pytest.mark.asyncio
async def test_split_router_sends_forced_inputs_to_lifecycle_graph():
    from wallet_agent.graph.build import build_graph

    model = ReactModel()
    graph = build_graph(model=model, split_graphs=True, require_react=True)
    result = await graph.ainvoke(
        {"forced_intent": "price_query", "intent": "price_query"},
        config={"configurable": {"thread_id": "lifecycle-route"}},
    )

    assert graph.lifecycle_graph.graph_role == "lifecycle"
    assert result["route_mode"] != "react"
    assert model.calls == []


@pytest.mark.asyncio
async def test_split_router_resumes_lifecycle_interrupt_after_rebuild():
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, NormalizedQuote, UnsignedTransaction
    from wallet_agent.graph.build import build_graph

    class Provider:
        provider_name = "bridgers"

        def __init__(self):
            self.prepare_calls = 0

        async def prepare(self, quote):
            self.prepare_calls += 1
            return UnsignedTransaction(
                chain=quote.source_asset.chain,
                to="0xrouter",
                data="0xdata",
                value="0",
                provider=self.provider_name,
                provider_reference=quote.provider_reference,
            )

    source = Asset(chain="BASE", symbol="USDC", decimals=6, address="0x1")
    destination = Asset(chain="BASE", symbol="USDT", decimals=6, address="0x2")
    quote = NormalizedQuote(
        provider="bridgers",
        source_asset=source,
        destination_asset=destination,
        input_amount=Decimal("1"),
        input_amount_raw="1000000",
        expected_output=Decimal("0.99"),
        expected_output_raw="990000",
        provider_reference="resume-quote",
    ).model_dump(mode="json")
    provider = Provider()
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "split-lifecycle-resume"}}
    initial = {
        "intent": "swap_prepare",
        "forced_intent": "swap_prepare",
        "selected_quote": quote,
        "swap_request": {
            "source_asset": source.model_dump(mode="json"),
            "destination_asset": destination.model_dump(mode="json"),
            "input_amount": "1",
            "input_amount_raw": "1000000",
            "sender_address": "0x1",
            "recipient_address": "0x2",
        },
    }
    first = build_graph(
        model=ReactModel(),
        providers=[provider],
        checkpointer=checkpointer,
        split_graphs=True,
        require_react=True,
    )

    paused = await first.ainvoke(initial, config=config)
    assert paused["__interrupt__"]
    assert provider.prepare_calls == 0

    rebuilt = build_graph(
        model=ReactModel(),
        providers=[provider],
        checkpointer=checkpointer,
        split_graphs=True,
        require_react=True,
    )
    resumed = await rebuilt.ainvoke(
        Command(resume={"approved": True}),
        config=config,
    )

    assert resumed["pending_transaction"]["to"] == "0xrouter"
    assert provider.prepare_calls == 1


@pytest.mark.asyncio
async def test_router_reads_lifecycle_checkpoint_when_confirmation_is_pending():
    from types import SimpleNamespace

    from wallet_agent.graph.build import GraphRouter

    class StateGraph:
        def __init__(self, state):
            self.state = state

        async def aget_state(self, _config):
            return self.state

        def get_state(self, _config):
            return self.state

    conversation_state = SimpleNamespace(next=(), tasks=(), values={"graph_role": "conversation"})
    lifecycle_state = SimpleNamespace(
        next=("confirmation_wait",),
        tasks=(SimpleNamespace(interrupts=(SimpleNamespace(value={"kind": "confirmation_required"}),)),),
        values={"confirmation_state": {"status": "requested"}, "graph_role": "lifecycle"},
    )
    router = GraphRouter(StateGraph(conversation_state), StateGraph(lifecycle_state), model=None)

    state = await router.aget_state({"configurable": {"thread_id": "pending-confirmation"}})

    assert state is lifecycle_state
