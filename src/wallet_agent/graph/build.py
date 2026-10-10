"""Build and compile the wallet LangGraph state machine."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.types import Command

from wallet_agent.observability import MetadataCache, RuntimeMetrics

from .nodes import GraphRuntime, make_nodes
from .routes import (
    route_after_confirmation,
    route_after_intent,
    route_after_resolution,
    route_after_status,
)
from .state import AgentState


class GraphRouter:
    """Stable graph facade routing chat and lifecycle inputs to separate graphs."""

    graph_role = "router"

    def __init__(self, conversation_graph: Any, lifecycle_graph: Any, model: Any) -> None:
        self.conversation_graph = conversation_graph
        self.lifecycle_graph = lifecycle_graph
        self._model = model
        self.graph_role = "router"

    @staticmethod
    def _is_lifecycle_input(value: Any) -> bool:
        if isinstance(value, Command):
            return True
        if not isinstance(value, Mapping):
            return True
        if value.get("forced_intent"):
            return True
        request = value.get("request")
        if not isinstance(request, Mapping) or not request.get("message"):
            return True
        return False

    def _select(self, value: Any) -> Any:
        return self.lifecycle_graph if self._is_lifecycle_input(value) else self.conversation_graph

    @staticmethod
    def _has_pending_lifecycle_work(state: Any) -> bool:
        """Identify a lifecycle checkpoint that must own the next resume.

        Split graphs share a checkpointer, but LangGraph keeps each compiled
        graph's pending tasks in its own checkpoint namespace.  Reading only
        the conversation graph can therefore hide a confirmation interrupt
        that is waiting in the lifecycle graph.
        """
        if not state:
            return False
        if tuple(getattr(state, "next", ()) or ()):
            return True
        return any(
            bool(getattr(task, "interrupts", ()))
            for task in (getattr(state, "tasks", ()) or ())
        )

    async def _state_graph(self, config: Any) -> Any:
        conversation_state = await self.conversation_graph.aget_state(config)
        lifecycle_state = await self.lifecycle_graph.aget_state(config)
        return (
            self.lifecycle_graph
            if self._has_pending_lifecycle_work(lifecycle_state)
            else self.conversation_graph
        )

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        graph = self._select(input)
        return await graph.ainvoke(input, config=config, **kwargs)

    def astream(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        graph = self._select(input)
        return graph.astream(input, config=config, **kwargs)

    def get_state(self, config: Any, **kwargs: Any) -> Any:
        conversation_state = self.conversation_graph.get_state(config, **kwargs)
        lifecycle_state = self.lifecycle_graph.get_state(config, **kwargs)
        if self._has_pending_lifecycle_work(lifecycle_state):
            return lifecycle_state
        return conversation_state

    async def aget_state(self, config: Any, **kwargs: Any) -> Any:
        conversation_state = await self.conversation_graph.aget_state(config, **kwargs)
        lifecycle_state = await self.lifecycle_graph.aget_state(config, **kwargs)
        if self._has_pending_lifecycle_work(lifecycle_state):
            return lifecycle_state
        return conversation_state

    async def aupdate_state(self, config: Any, values: Any, **kwargs: Any) -> Any:
        graph = await self._state_graph(config)
        return await graph.aupdate_state(config, values, **kwargs)

    @property
    def checkpointer(self) -> Any:
        return getattr(self.conversation_graph, "checkpointer", None)


def _resolve_checkpointer(checkpointer: Any) -> Any:
    """Pick the checkpointer to compile with.

    ``checkpointer=False`` is the platform seam: LangGraph Server injects its own
    checkpointer per run, and a compiled-in one shadows it, so the graph must be
    built without one. ``None`` keeps the local default.
    """
    if checkpointer is False:
        return None
    return checkpointer if checkpointer is not None else MemorySaver()


def _build_graph(
    *,
    model: Any,
    providers: Any = (),
    chains: Any = (),
    wallet_provider: Any | None = None,
    explorer_provider: Any | None = None,
    execution_observer: Any | None = None,
    price_provider: Any | None = None,
    # None -> an in-process MemorySaver (local default); False -> compile with
    # no checkpointer so LangGraph Server can inject its own.
    checkpointer: Any | None = None,
    max_poll_attempts: int = 3,
    confirmation_ttl_seconds: int = 900,
    provider_timeout_seconds: float = 15,
    max_react_steps: int = 8,
    react_tool_timeout_seconds: float = 20,
    require_react: bool = False,
    conversation_only: bool = False,
    lifecycle_only: bool = False,
    metadata_cache: MetadataCache | None = None,
    metrics: RuntimeMetrics | None = None,
) -> Any:
    """Compile a graph with injectable model, providers, chain adapters, and checkpointer."""
    if isinstance(providers, dict):
        provider_map = {str(k): v for k, v in providers.items()}
    else:
        provider_map = {
            str(getattr(item, "provider_name", "")): item
            for item in providers
            if getattr(item, "provider_name", None)
        }
    if isinstance(chains, dict):
        chain_map = {str(k).upper(): v for k, v in chains.items()}
    elif hasattr(chains, "items"):
        chain_map = {str(k).upper(): v for k, v in chains.items()}
    else:
        chain_map = {
            str(getattr(item, "chain", "")).upper(): item
            for item in chains
            if getattr(item, "chain", None)
        }
    runtime = GraphRuntime(
        model=model,
        providers=provider_map,
        chains=chain_map,
        wallet_provider=wallet_provider,
        explorer_provider=explorer_provider,
        execution_observer=execution_observer,
        price_provider=price_provider,
        max_poll_attempts=max(1, max_poll_attempts),
        confirmation_ttl_seconds=max(1, confirmation_ttl_seconds),
        provider_timeout_seconds=max(0.001, provider_timeout_seconds),
        max_react_steps=max(1, max_react_steps),
        react_tool_timeout_seconds=max(0.001, react_tool_timeout_seconds),
        require_react=require_react,
        metadata_cache=metadata_cache,
        metrics=metrics,
    )
    if metrics is not None and hasattr(model, "metrics"):
        model.metrics = metrics
    n = make_nodes(runtime)
    builder = StateGraph(AgentState)
    if not lifecycle_only:
        builder.add_node("react_entry", n["react_entry"])
        builder.add_node("react_agent", n["react_agent"])
        builder.add_node("react_tools", n["run_react_tools"])
        builder.add_node("finalize_response", n["finalize_response"])
    if conversation_only:
        builder.add_edge(START, "react_entry")
    elif lifecycle_only:
        builder.add_edge(START, "dialogue_manager")
    else:
        builder.add_conditional_edges(
            START,
            n["route_entry"],
            {"react": "react_entry", "legacy": "dialogue_manager"},
        )
    if not lifecycle_only:
        builder.add_edge("react_entry", "react_agent")
        builder.add_conditional_edges(
            "react_agent",
            tools_condition,
            {"tools": "react_tools", "__end__": "finalize_response"},
        )
        builder.add_edge("react_tools", "react_agent")
        builder.add_edge("finalize_response", END)
    if conversation_only:
        compiled = builder.compile(checkpointer=checkpointer)
        setattr(compiled, "_model", model)
        setattr(compiled, "graph_role", "conversation")
        setattr(compiled, "metadata_cache", metadata_cache)
        setattr(compiled, "metrics", metrics)
        return compiled
    builder.add_node("dialogue_manager", n["dialogue_manager"])
    builder.add_node("supervisor", n["supervisor"])
    builder.add_node("intent", n["intent"])
    builder.add_node("resolve_swap", n["resolve_swap"])
    builder.add_node("quote_provider", n["quote_provider"])
    # Provider quotes fan out via Send; defer the reducer until every branch
    # has completed so one invalid provider cannot overwrite valid candidates.
    builder.add_node("quote_response", n["quote_response"], defer=True)
    builder.add_node("asset_tool_call", n["asset_tool_call"])
    builder.add_node("asset_tool_result", n["asset_tool_result"])
    builder.add_node("confirmation_request", n["confirmation_request"])
    builder.add_node("confirmation_wait", n["confirmation_wait"])
    builder.add_node("wallet_query", n["wallet_query"])
    builder.add_node("wallet_tool_call", n["wallet_tool_call"])
    builder.add_node("wallet_tool_result", n["wallet_tool_result"])
    builder.add_node("transaction_tool_call", n["transaction_tool_call"])
    builder.add_node("transaction_tool_result", n["transaction_tool_result"])
    builder.add_node("gas_tool_call", n["gas_tool_call"])
    builder.add_node("gas_tool_result", n["gas_tool_result"])
    builder.add_node("price_query", n["price_query"])
    builder.add_node("transaction_status", n["transaction_status"])
    builder.add_node("portfolio_query", n["portfolio_query"])
    builder.add_node("gas_check", n["gas_check"])
    builder.add_node("asset_discovery", n["asset_discovery"])
    builder.add_node("transfer", n["transfer"])
    builder.add_node("swap_allowance", n["swap_allowance"])
    builder.add_node("prepare", n["prepare"])
    builder.add_node("register_broadcast", n["register_broadcast"])
    builder.add_node("status_poll", n["status_poll"])
    builder.add_node("response", n["response"])
    builder.add_node("read_only_agent", n["read_only_agent"])
    builder.add_node("read_only_tool", n["read_only_tool"])
    builder.add_node("read_only_observe", n["read_only_observe"])
    builder.add_edge("dialogue_manager", "supervisor")
    builder.add_edge("supervisor", "intent")
    builder.add_conditional_edges(
        "intent",
        route_after_intent,
        {
            "wallet_query": "wallet_tool_call",
            "transfer": "transfer",
            "confirmation_request": "confirmation_request",
            "swap_allowance": "swap_allowance",
            "price_query": "price_query",
            "transaction_status": "transaction_tool_call",
            "portfolio_query": "portfolio_query",
            "gas_check": "gas_tool_call",
            "asset_discovery": "asset_tool_call",
            "read_only": "read_only_agent",
            "swap_resolve": "resolve_swap",
            "swap_resolve_prepare": "resolve_swap",
            "status_poll": "status_poll",
            "register_broadcast": "register_broadcast",
            "response": "response",
        },
    )
    builder.add_conditional_edges(
        "read_only_agent",
        n["route_read_only"],
        {"tool": "read_only_tool", "agent": "read_only_agent", "response": "response"},
    )
    builder.add_edge("read_only_tool", "read_only_observe")
    builder.add_conditional_edges(
        "read_only_observe",
        n["route_read_only"],
        {"tool": "read_only_tool", "agent": "read_only_agent", "response": "response"},
    )
    builder.add_conditional_edges(
        "resolve_swap",
        route_after_resolution,
        ["quote_provider", "confirmation_request", "prepare", "response"],
    )
    builder.add_edge("confirmation_request", "confirmation_wait")
    builder.add_conditional_edges(
        "confirmation_wait",
        route_after_confirmation,
        {
            "swap_allowance": "swap_allowance",
            "response": "response",
            "supervisor": "supervisor",
        },
    )
    builder.add_edge("quote_provider", "quote_response")
    builder.add_edge("quote_response", END)
    builder.add_edge("asset_tool_call", "asset_tool_result")
    builder.add_edge("asset_tool_result", END)
    builder.add_edge("wallet_query", END)
    builder.add_edge("wallet_tool_call", "wallet_tool_result")
    builder.add_edge("wallet_tool_result", END)
    builder.add_edge("transaction_tool_call", "transaction_tool_result")
    builder.add_edge("transaction_tool_result", END)
    builder.add_edge("gas_tool_call", "gas_tool_result")
    builder.add_edge("gas_tool_result", END)
    builder.add_edge("price_query", END)
    builder.add_edge("transaction_status", END)
    builder.add_edge("portfolio_query", END)
    builder.add_edge("gas_check", END)
    builder.add_edge("asset_discovery", END)
    builder.add_edge("transfer", END)
    builder.add_edge("swap_allowance", END)
    builder.add_edge("prepare", END)
    builder.add_edge("register_broadcast", "status_poll")
    builder.add_edge("response", END)
    builder.add_conditional_edges(
        "status_poll", route_after_status, {"status_poll": "status_poll", "response": "response"}
    )
    compiled = builder.compile(checkpointer=checkpointer)
    # Keep the router available to the API transport for non-checkpointed
    # observability metadata. The model trace itself is emitted as SSE data.
    setattr(compiled, "_model", model)
    setattr(compiled, "graph_role", "lifecycle")
    setattr(compiled, "metadata_cache", metadata_cache)
    setattr(compiled, "metrics", metrics)
    return compiled


def build_graph(
    *,
    model: Any,
    providers: Any = (),
    chains: Any = (),
    wallet_provider: Any | None = None,
    explorer_provider: Any | None = None,
    execution_observer: Any | None = None,
    price_provider: Any | None = None,
    checkpointer: Any | None = None,
    max_poll_attempts: int = 3,
    confirmation_ttl_seconds: int = 900,
    provider_timeout_seconds: float = 15,
    max_react_steps: int = 8,
    react_tool_timeout_seconds: float = 20,
    require_react: bool = False,
    split_graphs: bool = False,
    conversation_only: bool = False,
    metadata_cache: MetadataCache | None = None,
    metadata_cache_ttl_seconds: float = 600,
    metadata_cache_max_entries: int = 512,
    metrics: RuntimeMetrics | None = None,
) -> Any:
    """Build the compatible graph or the production split graph router."""
    checkpointer = _resolve_checkpointer(checkpointer)
    if require_react and not callable(getattr(model, "invoke_react", None)):
        raise RuntimeError("configured model does not support the required ReAct contract")
    if require_react and getattr(model, "supports_react", True) is False:
        raise RuntimeError("configured model cannot bind wallet tools for ReAct")
    shared_cache = metadata_cache or MetadataCache(
        max_entries=metadata_cache_max_entries,
        ttl_seconds=metadata_cache_ttl_seconds,
    )
    shared_metrics = metrics or RuntimeMetrics()
    if not split_graphs:
        return _build_graph(
            model=model,
            providers=providers,
            chains=chains,
            wallet_provider=wallet_provider,
            explorer_provider=explorer_provider,
            execution_observer=execution_observer,
            price_provider=price_provider,
            checkpointer=checkpointer,
            max_poll_attempts=max_poll_attempts,
            confirmation_ttl_seconds=confirmation_ttl_seconds,
            provider_timeout_seconds=provider_timeout_seconds,
            max_react_steps=max_react_steps,
            react_tool_timeout_seconds=react_tool_timeout_seconds,
            require_react=require_react,
            conversation_only=conversation_only,
            metadata_cache=shared_cache,
            metrics=shared_metrics,
        )
    shared_checkpointer = checkpointer
    conversation = _build_graph(
        model=model,
        providers=providers,
        chains=chains,
        wallet_provider=wallet_provider,
        explorer_provider=explorer_provider,
        execution_observer=execution_observer,
        price_provider=price_provider,
        checkpointer=shared_checkpointer,
        max_poll_attempts=max_poll_attempts,
        confirmation_ttl_seconds=confirmation_ttl_seconds,
        provider_timeout_seconds=provider_timeout_seconds,
        max_react_steps=max_react_steps,
        react_tool_timeout_seconds=react_tool_timeout_seconds,
        require_react=True,
        conversation_only=True,
        metadata_cache=shared_cache,
        metrics=shared_metrics,
    )
    lifecycle = _build_graph(
        model=model,
        providers=providers,
        chains=chains,
        wallet_provider=wallet_provider,
        explorer_provider=explorer_provider,
        execution_observer=execution_observer,
        price_provider=price_provider,
        checkpointer=shared_checkpointer,
        max_poll_attempts=max_poll_attempts,
        confirmation_ttl_seconds=confirmation_ttl_seconds,
        provider_timeout_seconds=provider_timeout_seconds,
        max_react_steps=max_react_steps,
        react_tool_timeout_seconds=react_tool_timeout_seconds,
        require_react=False,
        lifecycle_only=True,
        metadata_cache=shared_cache,
        metrics=shared_metrics,
    )
    router = GraphRouter(conversation, lifecycle, model)
    router.metadata_cache = shared_cache
    router.metrics = shared_metrics
    return router
