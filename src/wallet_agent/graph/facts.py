"""Validation of model-generated read responses against verified graph facts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

_NUMBER = re.compile(
    r"(?<![A-Za-z0-9])\$?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)(?![A-Za-z0-9])"
)
_FORBIDDEN_STATUS_UPGRADES = ("已确认", "已到账", "confirmed", "completed", "received")


def _numbers(text: str) -> set[str]:
    return {match.group(1) for match in _NUMBER.finditer(text)}


def _same_decimal(left: str, right: str) -> bool:
    try:
        return Decimal(left.replace(",", "")) == Decimal(str(right).replace(",", ""))
    except (InvalidOperation, ValueError):
        return left == right


def _facts_numbers(facts: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("prices", "balances"):
        for item in facts.get(key) or []:
            if not isinstance(item, Mapping):
                continue
            for field in ("usd_price", "amount", "usd_value"):
                if item.get(field) is not None:
                    values.add(str(item[field]))
    return values


def validate_response_draft(
    draft: Mapping[str, Any], facts: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Return a JSON-safe draft only if it does not contradict verified facts."""
    if not isinstance(draft, Mapping) or not str(draft.get("message") or "").strip():
        return None
    message = str(draft["message"])
    verified_numbers = _facts_numbers(facts)
    if verified_numbers:
        for number in _numbers(message):
            if not any(_same_decimal(number, value) for value in verified_numbers):
                # Counts such as "2 assets" are not financial facts and may
                # be introduced by the response model from the card shape.
                count_values = {
                    str(len(facts.get("balances") or [])),
                    str(len(facts.get("prices") or [])),
                }
                if number not in count_values:
                    return None

    transaction = facts.get("transaction")
    if isinstance(transaction, Mapping):
        status = str(transaction.get("status") or "").lower()
        if status in {"pending", "processing", "unknown"} and any(
            phrase in message.lower() for phrase in _FORBIDDEN_STATUS_UPGRADES
        ):
            return None

    balances = {
        str(item.get("symbol")).upper(): str(item.get("amount"))
        for item in facts.get("balances") or []
        if isinstance(item, Mapping) and item.get("symbol") and item.get("amount") is not None
    }
    for suggestion in draft.get("suggestions") or []:
        if not isinstance(suggestion, Mapping):
            return None
        data = suggestion.get("data")
        if not isinstance(data, Mapping):
            return None
        amount = data.get("input_amount")
        symbol = str(data.get("source_symbol") or "").upper()
        if amount is not None and symbol in balances:
            try:
                if Decimal(str(amount)) > Decimal(balances[symbol]):
                    return None
            except (InvalidOperation, ValueError):
                return None
    return {str(key): value for key, value in draft.items()}
