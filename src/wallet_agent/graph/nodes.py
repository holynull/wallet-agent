"""Small graph nodes and runtime adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from langgraph.types import interrupt

from wallet_agent.chains._common import receipt_success
from wallet_agent.domain.models import (
    AgentError,
    ApprovalTransaction,
    NormalizedQuote,
    ProviderOrder,
    SwapQuoteRequest,
    TransferRequest,
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
            "transfer",
            "swap_select",
            "swap_allowance",
            "price_query",
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
        # Never silently choose a provider when multiple candidates exist.
        selected = quotes[0] if len(quotes) == 1 else state.get("selected_quote")
        if runtime.price_provider and quotes:
            assets = []
            for item in quotes:
                assets.extend([item.get("source_asset"), item.get("destination_asset")])
            try:
                from wallet_agent.domain.models import Asset

                parsed_assets = [Asset.model_validate(a) for a in assets if a]
                prices = await runtime.price_provider.get_prices(parsed_assets)
                price_map = {(p.asset.chain, p.asset.symbol, p.asset.address): p for p in prices}
                enriched = []
                for item in quotes:
                    q = dict(item)
                    source = q.get("source_asset", {})
                    destination = q.get("destination_asset", {})
                    src = price_map.get(
                        (source.get("chain"), source.get("symbol"), source.get("address"))
                    )
                    dst = price_map.get(
                        (
                            destination.get("chain"),
                            destination.get("symbol"),
                            destination.get("address"),
                        )
                    )
                    snaps = [p.model_dump(mode="json") for p in (src, dst) if p]
                    q["price_snapshots"] = snaps
                    if src:
                        q["usd_input_value"] = str(
                            Decimal(str(q.get("input_amount", 0))) * src.usd_price
                        )
                    if dst:
                        q["usd_expected_output"] = str(
                            Decimal(str(q.get("expected_output", 0))) * dst.usd_price
                        )
                    enriched.append(q)
                quotes = enriched
            except Exception:
                pass
        snapshot_map: dict[str, Any] = {}
        for item in quotes:
            raw_snapshots = item.get("price_snapshots") or []
            values = raw_snapshots.values() if isinstance(raw_snapshots, dict) else raw_snapshots
            for snapshot in values:
                snap = _dump(snapshot)
                if not isinstance(snap, dict):
                    continue
                asset = snap.get("asset") or {}
                key = ":".join(
                    str(asset.get(part) or "")
                    for part in ("chain", "chain_id", "symbol", "address")
                )
                snapshot_map[key] = snap
        return {
            "selected_quote": selected,
            "quote_candidates": quotes,
            "response": {
                "kind": "swap_quote",
                "quotes": quotes,
                "price_snapshots": snapshot_map,
            },
        }

    async def transfer(state: dict[str, Any]) -> dict[str, Any]:
        raw = (
            state.get("request", {}).get("transfer_request")
            or state.get("transfer_request")
            or state.get("request", {})
        )
        try:
            req = TransferRequest.model_validate(raw)
        except Exception as exc:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("INVALID_TRANSFER_PARAMETERS", str(exc))],
                }
            }
        adapter = runtime.chains.get(req.chain.upper())
        if adapter is None:
            return {
                "response": {
                    "kind": "error",
                    "errors": [
                        _error("CHAIN_CAPABILITY_UNAVAILABLE", f"Chain {req.chain} is unavailable.")
                    ],
                }
            }
        try:
            if req.token is None:
                balance = await adapter.get_native_balance(req.sender)
                if int(balance.amount_raw) < int(req.amount_raw):
                    return {
                        "response": {
                            "kind": "error",
                            "errors": [
                                _error("INSUFFICIENT_BALANCE", "Insufficient native balance.")
                            ],
                        }
                    }
                tx = adapter.build_native_transfer(
                    from_address=req.sender, to_address=req.recipient, amount_raw=req.amount_raw
                )
            else:
                balance = await adapter.get_token_balance(req.token, req.sender)
                if int(balance.amount_raw) < int(req.amount_raw):
                    return {
                        "response": {
                            "kind": "error",
                            "errors": [
                                _error("INSUFFICIENT_BALANCE", "Insufficient token balance.")
                            ],
                        }
                    }
                tx = adapter.build_erc20_transfer(
                    token=req.token,
                    from_address=req.sender,
                    to_address=req.recipient,
                    amount_raw=req.amount_raw,
                )
            return {
                "pending_transaction": tx.model_dump(mode="json"),
                "response": {
                    "kind": "transfer_prepare",
                    "pending_transaction": tx.model_dump(mode="json"),
                },
            }
        except Exception as exc:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("TRANSFER_PREPARE_FAILED", str(exc))],
                }
            }

    async def swap_allowance(state: dict[str, Any]) -> dict[str, Any]:
        selected = state.get("selected_quote")
        if not selected:
            return {
                "response": {
                    "stage": "quote_selection_required",
                    "quotes": state.get("quote_candidates", []),
                }
            }
        quote = _quote(selected)
        requirement = quote.allowance_requirement
        if requirement is None:
            # Native swaps do not need approval; prepare directly.
            provider = runtime.providers.get(str(selected.get("provider")))
            if provider is None:
                return {
                    "response": {
                        "stage": "failed",
                        "errors": [_error("PROVIDER_UNAVAILABLE", "Provider unavailable")],
                    }
                }
            prepared = await provider.prepare(quote)
            dumped = _dump(prepared)
            return {
                "pending_transaction": dumped,
                "authorization_stage": "swap_ready",
                "response": {"stage": "swap_ready", "pending_transaction": dumped},
            }
        adapter = runtime.chains.get(requirement.token.chain.upper())
        if adapter is None:
            return {
                "response": {
                    "stage": "failed",
                    "errors": [_error("CHAIN_CAPABILITY_UNAVAILABLE", "Chain adapter unavailable")],
                }
            }
        allowance_raw = await adapter.get_allowance(
            requirement.token, requirement.owner, requirement.spender
        )
        required = int(requirement.required_amount_raw)
        if int(allowance_raw) < required and not state.get("approval_tx_hash"):
            approval = adapter.build_erc20_approve(
                token=requirement.token,
                owner=requirement.owner,
                spender=requirement.spender,
                amount_raw=requirement.required_amount_raw,
            )
            approval_model = ApprovalTransaction(
                chain=requirement.token.chain,
                chain_id=requirement.token.chain_id,
                to=approval.to,
                data=approval.data,
                value=approval.value,
                token=requirement.token,
                owner=requirement.owner,
                spender=requirement.spender,
                amount_raw=requirement.required_amount_raw,
            )
            answer = interrupt(
                {
                    "kind": "approval_required",
                    "approval_transaction": approval_model.model_dump(mode="json"),
                }
            )
            if isinstance(answer, dict):
                h = answer.get("approve_tx_hash") or answer.get("tx_hash")
                if h:
                    return {
                        "approval_transaction": approval_model.model_dump(mode="json"),
                        "approval_tx_hash": str(h),
                        "allowance_requirement": requirement.model_dump(mode="json"),
                        "authorization_stage": "approval_submitted",
                    }
            return {
                "approval_transaction": approval_model.model_dump(mode="json"),
                "allowance_requirement": requirement.model_dump(mode="json"),
                "authorization_stage": "approval_required",
                "response": {
                    "stage": "approval_required",
                    "approval_transaction": approval_model.model_dump(mode="json"),
                },
            }
        if state.get("approval_tx_hash"):
            receipt = await adapter.get_transaction_receipt(str(state["approval_tx_hash"]))
            success = receipt_success(receipt)
            if success is False or receipt is None:
                return {
                    "response": {
                        "stage": "approval_pending" if receipt is None else "approval_failed",
                        "approval_transaction": state.get("approval_transaction"),
                    }
                }
            allowance_raw = await adapter.get_allowance(
                requirement.token, requirement.owner, requirement.spender
            )
            if int(allowance_raw) < required:
                return {
                    "response": {
                        "stage": "approval_required",
                        "approval_transaction": state.get("approval_transaction"),
                    }
                }
        provider = runtime.providers.get(str(selected.get("provider")))
        if provider is None:
            return {
                "response": {
                    "stage": "failed",
                    "errors": [_error("PROVIDER_UNAVAILABLE", "Provider unavailable")],
                }
            }
        prepared = await provider.prepare(quote)
        dumped = _dump(prepared)
        return {
            "pending_transaction": dumped,
            "authorization_stage": "swap_ready",
            "response": {"stage": "swap_ready", "pending_transaction": dumped},
        }

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

    async def price_query(state: dict[str, Any]) -> dict[str, Any]:
        """Resolve a token/native USD price through the injected provider."""
        if runtime.price_provider is None:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("PRICE_PROVIDER_UNAVAILABLE", "Price provider unavailable.")],
                }
            }
        raw = state.get("price_request") or state.get("request", {}).get("price_request")
        if raw is None:
            return {
                "response": {
                    "kind": "clarification",
                    "errors": [
                        _error("MISSING_PRICE_PARAMETERS", "Price parameters are required.")
                    ],
                }
            }
        try:
            from wallet_agent.domain.models import Asset

            asset = Asset.model_validate(raw)
            prices = await runtime.price_provider.get_prices([asset])
            return {"response": {"kind": "price_query", "prices": [_dump(item) for item in prices]}}
        except Exception as exc:
            return {
                "response": {
                    "kind": "error",
                    "errors": [_error("PRICE_QUERY_FAILED", str(exc), retryable=True)],
                }
            }

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
