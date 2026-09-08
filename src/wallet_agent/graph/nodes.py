"""Small graph nodes and runtime adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from langgraph.types import interrupt

from wallet_agent.domain.models import (
    AgentError,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
)


@dataclass(frozen=True)
class GraphRuntime:
    model: Any
    providers: dict[str, Any]
    chains: dict[str, Any]
    price_provider: Any | None = None
    max_poll_attempts: int = 3


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _quote(value: Any) -> NormalizedQuote:
    return value if isinstance(value, NormalizedQuote) else NormalizedQuote.model_validate(value)


def _request(value: Any) -> SwapQuoteRequest:
    return value if isinstance(value, SwapQuoteRequest) else SwapQuoteRequest.model_validate(value)


def _error(
    code: str, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return AgentError(
        code=code, message=message, retryable=retryable, details=details or {}
    ).model_dump(mode="json")


def _parse_model_output(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        result = value.model_dump()
        return result if isinstance(result, dict) else None
    if isinstance(value, str):
        try:
            result = json.loads(value)
        except (TypeError, ValueError):
            return None
        return result if isinstance(result, dict) else None
    return None


def make_nodes(runtime: GraphRuntime) -> dict[str, Any]:
    async def intent(state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("intent")
        valid = {
            "wallet_query",
            "swap_quote",
            "swap_prepare",
            "swap_status",
            "clarification",
            "unsupported",
        }
        if existing in valid:
            return {"route": existing, "max_poll_attempts": runtime.max_poll_attempts}
        request = state.get("request", {})
        try:
            output = (
                runtime.model.ainvoke(request)
                if hasattr(runtime.model, "ainvoke")
                else runtime.model.invoke(request)
            )
            output = await output if hasattr(output, "__await__") else output
        except Exception as exc:  # model failures are user-actionable clarification
            return {
                "intent": "clarification",
                "errors": [_error("MODEL_OUTPUT_INVALID", str(exc))],
                "route": "clarification",
                "max_poll_attempts": runtime.max_poll_attempts,
            }
        parsed = _parse_model_output(output)
        if not parsed or parsed.get("intent") not in valid:
            return {
                "intent": "clarification",
                "errors": [
                    _error(
                        "MODEL_OUTPUT_INVALID",
                        "The assistant could not determine the requested action.",
                    )
                ],
                "route": "clarification",
                "max_poll_attempts": runtime.max_poll_attempts,
            }
        return {
            "intent": parsed["intent"],
            "route": parsed["intent"],
            "max_poll_attempts": runtime.max_poll_attempts,
        }

    async def resolve_swap(state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("swap_request") or state.get("request", {}).get("swap_request")
        if raw is None:
            return {
                "intent": "clarification",
                "errors": [_error("MISSING_SWAP_PARAMETERS", "Swap parameters are required.")],
            }
        try:
            request = _request(raw)
        except Exception as exc:
            return {
                "intent": "clarification",
                "errors": [_error("INVALID_SWAP_PARAMETERS", str(exc))],
            }
        selected = state.get("selected_quote")
        if selected is None and state.get("quote_candidates"):
            selected = state["quote_candidates"][0]
        return {
            "swap_request": request.model_dump(mode="json"),
            "available_providers": list(runtime.providers),
            "selected_quote": selected,
        }

    async def quote_provider(state: dict[str, Any]) -> dict[str, Any]:
        name = state.get("provider_name") or state.get("request", {}).get("_provider_name")
        request = state.get("swap_request")
        provider = runtime.providers.get(str(name))
        if provider is None or request is None:
            return {
                "quote_candidates": [],
                "errors": [_error("PROVIDER_UNAVAILABLE", f"Provider {name} is unavailable.")],
            }
        try:
            quote = await provider.quote(_request(request))
            return {"quote_candidates": [_dump(quote)]}
        except Exception as exc:
            return {
                "quote_candidates": [],
                "errors": [
                    _error("PROVIDER_QUOTE_FAILED", str(exc), details={"provider": str(name)})
                ],
            }

    async def quote_response(state: dict[str, Any]) -> dict[str, Any]:
        quotes = state.get("quote_candidates", [])
        if not quotes and state.get("errors"):
            return {"response": {"kind": "error", "errors": state["errors"]}}
        selected = (
            min(quotes, key=lambda item: str(item.get("provider_fee") or "0")) if quotes else None
        )
        return {"selected_quote": selected, "response": {"kind": "swap_quote", "quotes": quotes}}

    async def wallet_query(state: dict[str, Any]) -> dict[str, Any]:
        request = state.get("request", {})
        chain = str(request.get("chain", ""))
        address = str(request.get("address", ""))
        adapter = runtime.chains.get(chain.upper())
        if adapter is None:
            return {
                "errors": [
                    _error(
                        "CHAIN_CAPABILITY_UNAVAILABLE",
                        f"Chain {chain} is unavailable.",
                        details={"chain": chain},
                    )
                ]
            }
        try:
            if hasattr(adapter, "validate_address") and not await adapter.validate_address(address):
                return {"errors": [_error("INVALID_ADDRESS", "The wallet address is invalid.")]}
            snapshot = {"address": address, "chain": chain}
            if hasattr(adapter, "get_native_balance"):
                snapshot["native_balance"] = _dump(await adapter.get_native_balance(address))
            if hasattr(adapter, "get_token_balances"):
                snapshot["token_balances"] = [
                    _dump(item) for item in await adapter.get_token_balances(address)
                ]
            return {
                "wallet_context": snapshot,
                "response": {"kind": "wallet_query", "wallet": snapshot},
            }
        except Exception as exc:
            return {"errors": [_error("WALLET_QUERY_FAILED", str(exc))]}

    async def prepare(state: dict[str, Any]) -> dict[str, Any]:
        selected = state.get("selected_quote")
        if selected is None:
            return {
                "response": {
                    "kind": "clarification",
                    "message": "A quote is required before preparation.",
                }
            }
        if state.get("pending_transaction"):
            return {}
        confirmation = state.get("user_confirmation")
        if confirmation is None:
            answer = interrupt({"kind": "confirmation_required", "quote": selected})
            if isinstance(answer, dict):
                confirmation = answer
            elif answer is True:
                confirmation = {"approved": True}
            else:
                confirmation = {"approved": False}
        if not confirmation.get("approved", False):
            return {
                "user_confirmation": confirmation,
                "response": {"kind": "cancelled", "message": "Swap cancelled."},
            }
        provider_name = str(selected.get("provider"))
        provider = runtime.providers.get(provider_name)
        if provider is None:
            return {
                "errors": [
                    _error("PROVIDER_UNAVAILABLE", f"Provider {provider_name} is unavailable.")
                ]
            }
        try:
            prepared = await provider.prepare(_quote(selected))
            return {
                "user_confirmation": confirmation,
                "pending_transaction": _dump(prepared),
                "response": {"kind": "swap_prepare", "transaction": _dump(prepared)},
            }
        except Exception as exc:
            return {
                "errors": [
                    _error("PROVIDER_PREPARE_FAILED", str(exc), details={"provider": provider_name})
                ]
            }

    async def register_broadcast(state: dict[str, Any]) -> dict[str, Any]:
        tx_hash = state.get("broadcast_tx_hash")
        selected = state.get("selected_quote") or {}
        provider_name = str(selected.get("provider", ""))
        if not tx_hash or not provider_name:
            return {
                "errors": [
                    _error(
                        "MISSING_BROADCAST_HASH", "A chain-qualified transaction hash is required."
                    )
                ]
            }
        orders = state.get("provider_orders", {})
        if provider_name in orders:
            return {
                "provider_order_ids": {provider_name: orders[provider_name]["provider_order_id"]}
            }
        provider = runtime.providers.get(provider_name)
        if provider is None:
            return {
                "errors": [
                    _error("PROVIDER_UNAVAILABLE", f"Provider {provider_name} is unavailable.")
                ]
            }
        try:
            order = await provider.register_broadcast(
                selected.get("provider_reference", ""), tx_hash
            )
            value = _dump(order)
            return {
                "provider_order_ids": {provider_name: order.provider_order_id},
                "provider_orders": {provider_name: value},
            }
        except Exception as exc:
            return {
                "errors": [
                    _error(
                        "PROVIDER_REGISTER_FAILED", str(exc), details={"provider": provider_name}
                    )
                ]
            }

    async def status_poll(state: dict[str, Any]) -> dict[str, Any]:
        orders = state.get("provider_orders", {})
        if not orders:
            return {
                "response": {"kind": "swap_status", "status": "unknown"},
                "poll_attempts": state.get("poll_attempts", 0) + 1,
            }
        provider_name, raw_order = next(iter(orders.items()))
        provider = runtime.providers.get(provider_name)
        if provider is None:
            return {
                "errors": [
                    _error("PROVIDER_UNAVAILABLE", f"Provider {provider_name} is unavailable.")
                ]
            }
        try:
            status = await provider.get_status(ProviderOrder.model_validate(raw_order))
            dumped = _dump(status)
            attempts = state.get("poll_attempts", 0) + 1
            terminal = {
                "completed",
                "failed",
                "refunded",
                "timed_out",
                "kyc_required",
                "cancelled",
            }
            if dumped.get("status") not in terminal and attempts >= state.get(
                "max_poll_attempts", runtime.max_poll_attempts
            ):
                dumped = {**dumped, "status": "timed_out"}
            return {
                "status_snapshot": dumped,
                "poll_attempts": attempts,
                "response": {"kind": "swap_status", "status": dumped},
            }
        except Exception as exc:
            return {
                "errors": [
                    _error("PROVIDER_STATUS_FAILED", str(exc), details={"provider": provider_name})
                ],
                "poll_attempts": state.get("poll_attempts", 0) + 1,
            }

    async def response(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("response"):
            return {}
        if state.get("intent") == "clarification":
            return {"response": {"kind": "clarification", "errors": state.get("errors", [])}}
        if state.get("errors"):
            return {"response": {"kind": "error", "errors": state["errors"]}}
        return {"response": {"kind": state.get("intent", "clarification")}}

    return locals()
