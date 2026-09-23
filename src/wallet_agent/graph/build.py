"""Build and compile the wallet LangGraph state machine."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .nodes import GraphRuntime, make_nodes
from .routes import (
    route_after_confirmation,
    route_after_intent,
    route_after_resolution,
    route_after_status,
)
from .state import AgentState


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
    )
    n = make_nodes(runtime)
    builder = StateGraph(AgentState)
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
    builder.add_edge(START, "supervisor")
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
            "swap_resolve": "resolve_swap",
            "swap_resolve_prepare": "resolve_swap",
            "status_poll": "status_poll",
            "register_broadcast": "register_broadcast",
            "response": "response",
        },
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
    return builder.compile(checkpointer=checkpointer or MemorySaver())
