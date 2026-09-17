"""Canonical identifiers shared across user input and provider catalogs."""

from __future__ import annotations

import re

_CHAIN_ALIASES = {
    "ETH": "ETH",
    "ETHEREUM": "ETH",
    "ETHEREUMMAINNET": "ETH",
    "ETHMAINNET": "ETH",
    "ERC20": "ETH",
    "以太": "ETH",
    "以太坊": "ETH",
    "以太坊主网": "ETH",
    "BSC": "BSC",
    "BEP20": "BSC",
    "BINANCESMARTCHAIN": "BSC",
    "BNBSMARTCHAIN": "BSC",
    "币安链": "BSC",
    "币安智能链": "BSC",
    "ARBITRUMONE": "ARBITRUM",
    "OPMAINNET": "OPTIMISM",
    "波场": "TRON",
    "索拉纳": "SOLANA",
}
_CHAIN_IDS = {
    "ETH": 1,
    "BSC": 56,
    "BASE": 8453,
    "ARBITRUM": 42161,
    "OPTIMISM": 10,
    "POLYGON": 137,
}
_SYMBOL_NOISE = re.compile(r"[\s'\-`\u2018\u2019]+")
_PROVIDER_NETWORK_SUFFIX = re.compile(r"\((?:ERC20|BEP20|TRC20)\)$")
_AMOUNT_WITH_UNIT = re.compile(
    r"^\s*((?:\d+(?:\.\d*)?|\.\d+))\s*[A-Za-z][A-Za-z0-9()'`\-]*\s*$"
)
_AMOUNT_ONLY = re.compile(r"^\s*((?:\d+(?:\.\d*)?|\.\d+))\s*$")


def canonical_chain(value: str) -> str:
    """Return the chain identifier used by adapters and provider APIs."""
    normalized = " ".join(str(value).strip().upper().replace("_", " ").split())
    compact = normalized.replace(" ", "").replace("-", "")
    return _CHAIN_ALIASES.get(compact, normalized)


def canonical_symbol(value: str) -> str:
    """Remove harmless user noise and provider network suffixes from a symbol."""
    normalized = _SYMBOL_NOISE.sub("", str(value)).upper()
    return _PROVIDER_NETWORK_SUFFIX.sub("", normalized)


def canonical_amount(value: str) -> str:
    """Remove one unambiguous token unit while preserving uncertain text."""
    return unambiguous_amount(value) or str(value).strip()


def unambiguous_amount(value: str) -> str | None:
    """Extract an amount only when the complete text is numeric with an optional unit."""
    raw = str(value)
    match = _AMOUNT_ONLY.fullmatch(raw) or _AMOUNT_WITH_UNIT.fullmatch(raw)
    return match.group(1) if match else None


def chain_id_for(value: str) -> int | None:
    return _CHAIN_IDS.get(canonical_chain(value))
