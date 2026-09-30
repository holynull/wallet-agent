"""Bounded, fixed-registry read-only tool loop helpers."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Awaitable, Callable, Literal

READ_ONLY_TOOL_NAMES = frozenset(
    {
        "wallet_balance",
        "price_lookup",
        "gas_estimate",
        "asset_discovery",
        "transaction_status",
        "market_details",
        "price_history",
        "portfolio_snapshot",
    }
)
READ_ONLY_MAX_ROUNDS = 4


@dataclass(frozen=True)
class ReadOnlyToolCall:
    name: str
    arguments: dict[str, Any]


def route_read_only(state: dict[str, Any]) -> Literal["agent", "tool", "response"]:
    count = int(state.get("read_loop_count") or 0)
    if count >= READ_ONLY_MAX_ROUNDS or state.get("read_only_complete"):
        return "response"
    if state.get("read_tool_call"):
        return "tool"
    return "agent"


def read_only_tool_name_for_intent(intent: str) -> str | None:
    return {
        "wallet_query": "wallet_balance",
        "price_query": "price_lookup",
        "gas_check": "gas_estimate",
        "asset_discovery": "asset_discovery",
        "transaction_status": "transaction_status",
        "market_details": "market_details",
        "price_history": "price_history",
        "portfolio_query": "portfolio_snapshot",
    }.get(intent)


async def run_read_only_round(
    state: dict[str, Any],
    call: ReadOnlyToolCall,
    tools: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]],
) -> dict[str, Any]:
    count = int(state.get("read_loop_count") or 0) + 1
    calls = [dict(item) for item in state.get("read_tool_calls") or []]
    facts = dict(state.get("read_facts") or {})
    started = perf_counter()
    record: dict[str, Any] = {
        "name": call.name,
        "args": dict(call.arguments),
        "status": "started",
    }
    calls.append(record)
    if call.name not in READ_ONLY_TOOL_NAMES or call.name not in tools:
        record.update(
            status="rejected",
            error={"code": "READ_TOOL_NOT_ALLOWED", "message": "Read-only tool is not allowed."},
        )
        facts["error"] = record["error"]
        return {"read_loop_count": count, "read_tool_calls": calls, "read_facts": facts}
    try:
        observation = await tools[call.name](dict(call.arguments))
        if not isinstance(observation, dict):
            raise TypeError("read-only tools must return a JSON object")
        facts[call.name] = observation
        record.update(status="completed", elapsed_ms=round((perf_counter() - started) * 1000, 2))
    except Exception as exc:
        record.update(
            status="failed",
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
            error={"code": "READ_TOOL_FAILED", "message": str(exc)},
        )
        facts["error"] = record["error"]
    return {"read_loop_count": count, "read_tool_calls": calls, "read_facts": facts}


async def read_only_decision(model: Any, request: dict[str, Any]) -> ReadOnlyToolCall | None:
    """Ask an opt-in model capability for a validated read tool call."""
    planner = getattr(model, "plan_read", None)
    if not callable(planner):
        return None
    output = planner(request)
    if hasattr(output, "__await__"):
        output = await output
    if hasattr(output, "model_dump"):
        output = output.model_dump(mode="json")
    if not isinstance(output, dict):
        return None
    name = output.get("name")
    arguments = output.get("arguments")
    if name not in READ_ONLY_TOOL_NAMES or not isinstance(arguments, dict):
        return None
    return ReadOnlyToolCall(name=name, arguments=dict(arguments))
