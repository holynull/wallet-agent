"""Pure helpers for durable conversational wallet tasks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .state import ActiveTask, SlotSource, TaskKind

_TRANSFER_TO_LEGACY = {
    "chain": "transfer_chain",
    "symbol": "transfer_symbol",
    "token_address": "transfer_token_address",
    "decimals": "transfer_decimals",
    "amount": "transfer_amount",
    "amount_raw": "transfer_amount_raw",
    "sender": "transfer_sender",
    "recipient": "transfer_recipient",
}
_TRANSFER_FROM_LEGACY = {legacy: canonical for canonical, legacy in _TRANSFER_TO_LEGACY.items()}


@dataclass(frozen=True)
class TaskMergeResult:
    task: ActiveTask
    changed_slots: frozenset[str]
    invalidation: dict[str, Any]


def new_active_task(kind: TaskKind, *, task_id: str | None = None) -> ActiveTask:
    return {
        "task_id": task_id or str(uuid4()),
        "kind": kind,
        "status": "collecting",
        "stage": "collecting_slots",
        "revision": 0,
        "slots": {},
        "slot_sources": {},
        "missing_fields": [],
        "updated_by": "system",
    }


def hydrate_active_task(state: Mapping[str, Any]) -> ActiveTask | None:
    raw_active = state.get("active_task")
    if isinstance(raw_active, Mapping) and raw_active.get("kind") in {"transfer", "swap"}:
        return _copy_task(raw_active)

    goal = (state.get("conversation_state") or {}).get("goal")
    candidates = (
        ("transfer", state.get("transfer_draft")),
        ("swap", state.get("swap_draft")),
    )
    if goal in {"transfer", "swap"}:
        candidates = tuple(sorted(candidates, key=lambda item: item[0] != goal))
    for kind, raw_draft in candidates:
        if not isinstance(raw_draft, Mapping) or not raw_draft:
            continue
        slots = _canonical_slots(kind, raw_draft)
        conversation_id = str(state.get("conversation_id") or "legacy")
        task = new_active_task(
            kind, task_id=str(uuid5(NAMESPACE_URL, f"wallet-agent:{conversation_id}:{kind}"))
        )
        task.update(
            revision=1,
            slots=slots,
            slot_sources={key: "legacy" for key in slots},
            updated_by="system",
        )
        return task
    return None


def merge_task_patch(
    task: ActiveTask,
    patch: Mapping[str, Any] | Any,
    *,
    source: SlotSource = "user",
) -> TaskMergeResult:
    updated = _copy_task(task)
    raw_patch = patch.model_dump(exclude_none=True) if hasattr(patch, "model_dump") else dict(patch)
    normalized = {str(key): value for key, value in raw_patch.items() if value is not None}
    slots = dict(updated.get("slots") or {})
    changed = frozenset(key for key, value in normalized.items() if slots.get(key) != value)
    if not changed:
        return TaskMergeResult(updated, changed, {})

    slots.update({key: normalized[key] for key in changed})
    _clear_derived_slots(updated["kind"], slots, changed, normalized)
    sources = dict(updated.get("slot_sources") or {})
    sources.update({key: source for key in changed})
    for key in tuple(sources):
        if key not in slots:
            sources.pop(key, None)
    updated.update(
        revision=int(updated.get("revision") or 0) + 1,
        slots=slots,
        slot_sources=sources,
        updated_by="user" if source == "user" else "system",
    )
    return TaskMergeResult(updated, changed, task_invalidation(updated["kind"], changed))


def project_legacy_draft(task: ActiveTask) -> dict[str, Any]:
    slots = dict(task.get("slots") or {})
    if task.get("kind") == "transfer":
        return {
            legacy: slots[canonical]
            for canonical, legacy in _TRANSFER_TO_LEGACY.items()
            if canonical in slots
        }
    return slots


def task_invalidation(kind: TaskKind, changed_slots: frozenset[str]) -> dict[str, Any]:
    if not changed_slots:
        return {}
    common = {
        "confirmation_state": None,
        "pending_transaction": None,
        "preflight": None,
    }
    if kind == "transfer":
        return {**common, "transfer_request": None}
    return {
        "selected_quote": None,
        "quote_candidates": [{"__clear__": True}],
        **common,
        "swap_request": None,
        "approval_transaction": None,
        "allowance_requirement": None,
    }


def _canonical_slots(kind: TaskKind, draft: Mapping[str, Any]) -> dict[str, Any]:
    if kind == "transfer":
        return {
            _TRANSFER_FROM_LEGACY.get(str(key), str(key)): value
            for key, value in draft.items()
            if value is not None
        }
    return {str(key): value for key, value in draft.items() if value is not None}


def _copy_task(task: Mapping[str, Any]) -> ActiveTask:
    return {
        **dict(task),
        "slots": dict(task.get("slots") or {}),
        "slot_sources": dict(task.get("slot_sources") or {}),
        "missing_fields": list(task.get("missing_fields") or []),
    }


def _clear_derived_slots(
    kind: TaskKind,
    slots: dict[str, Any],
    changed: frozenset[str],
    patch: Mapping[str, Any],
) -> None:
    if kind == "transfer":
        if changed & {"chain", "symbol", "token_address"} and "decimals" not in patch:
            slots.pop("decimals", None)
        if "amount" in changed and "amount_raw" not in patch:
            slots.pop("amount_raw", None)
        return

    for side in ("source", "destination"):
        if changed & {f"{side}_chain", f"{side}_symbol"}:
            if f"{side}_token_address" not in patch:
                slots.pop(f"{side}_token_address", None)
            slots.pop(f"{side}_decimals", None)
            slots.pop(f"{side}_chain_id", None)
        elif f"{side}_token_address" in changed:
            slots.pop(f"{side}_decimals", None)
    if "input_amount" in changed and "input_amount_raw" not in patch:
        slots.pop("input_amount_raw", None)
