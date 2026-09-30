"""Deterministic resolution of references to previously displayed read results."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_ASSET_SYMBOL = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,11})(?![A-Za-z0-9])")
_QUOTE_ORDINALS = {
    "第一个": 1,
    "第一": 1,
    "1": 1,
    "first": 1,
    "第二个": 2,
    "第二": 2,
    "2": 2,
    "second": 2,
    "第三个": 3,
    "第三": 3,
    "3": 3,
    "third": 3,
}


def _reference_stub(referent: Mapping[str, Any]) -> dict[str, Any]:
    return {key: referent[key] for key in ("kind", "id") if referent.get(key) is not None}


def resolve_read_reference(
    message: str, dialogue: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Resolve a read-only follow-up only when its previous referent is unambiguous."""
    if not isinstance(dialogue, Mapping):
        return None
    referent = dialogue.get("referent")
    if not isinstance(referent, Mapping):
        return None
    facts = referent.get("facts")
    if not isinstance(facts, Mapping):
        return None
    text = str(message or "").strip()
    lowered = text.lower()

    if referent.get("kind") == "asset" and dialogue.get("active_topic") == "price":
        symbols = [match.upper() for match in _ASSET_SYMBOL.findall(text)]
        symbols = [symbol for symbol in symbols if symbol not in {"BSC", "BASE"}]
        if len(symbols) != 1:
            return None
        symbol = symbols[0]
        return {
            "intent": "price_query",
            "referent": _reference_stub(referent),
            "arguments": {"chain": facts.get("chain"), "symbol": symbol},
        }

    if referent.get("kind") == "transaction" and any(
        token in lowered for token in ("刚才那笔", "这笔交易", "到账了吗", "transaction", "that tx")
    ):
        if not facts.get("chain") or not facts.get("tx_hash"):
            return None
        return {
            "intent": "transaction_status",
            "referent": _reference_stub(referent),
            "arguments": {"chain": facts["chain"], "tx_hash": facts["tx_hash"]},
        }

    if referent.get("kind") == "quote" and any(
        token in lowered
        for token in ("哪个报价", "更划算", "best quote", "which quote", "比较报价")
    ):
        quotes = facts.get("quotes")
        if not isinstance(quotes, list) or not quotes:
            return None
        return {
            "intent": "quote_compare",
            "referent": _reference_stub(referent),
            "arguments": {"quotes": quotes},
        }
    return None


def resolve_quote_reference(
    message: str, referent: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Map an ordinal to a displayed quote without approving or preparing it."""
    if not isinstance(referent, Mapping) or referent.get("kind") != "quote":
        return None
    facts = referent.get("facts")
    quotes = facts.get("quotes") if isinstance(facts, Mapping) else None
    if not isinstance(quotes, list):
        return None
    text = str(message or "").strip().lower()
    ordinal = next(
        (index for phrase, index in _QUOTE_ORDINALS.items() if phrase in text),
        None,
    )
    if ordinal is None or ordinal > len(quotes):
        return None
    selected = quotes[ordinal - 1]
    if not isinstance(selected, Mapping):
        return None
    return {
        "intent": "swap_quote",
        "referent": _reference_stub(referent),
        "arguments": {"selected_quote": dict(selected)},
        "requires_confirmation": True,
    }
