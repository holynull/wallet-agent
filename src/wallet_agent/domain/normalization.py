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
    r"^\s*((?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z][A-Za-z0-9()'`\-]*)\s*$"
)
_AMOUNT_WITH_UNIT_MENTION = re.compile(
    r"(?<![A-Za-z0-9_.])((?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"([A-Za-z][A-Za-z0-9()'`\-]*)"
)
_AMOUNT_ONLY = re.compile(r"^\s*((?:\d+(?:\.\d*)?|\.\d+))\s*$")
_TRANSACTION_HASH_MENTION = re.compile(
    r"(?<![0-9a-f])0x[0-9a-f]{64}(?![0-9a-f])", re.IGNORECASE
)
_TRANSACTION_CHAIN_MENTIONS = (
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:ethereum|erc20|eth)(?![A-Za-z0-9])|"
            r"以太坊主网|以太坊|以太",
            re.IGNORECASE,
        ),
        "ETH",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:binance\s*smart\s*chain|bsc|bep20)"
            r"(?![A-Za-z0-9])|币安智能链|币安链",
            re.IGNORECASE,
        ),
        "BSC",
    ),
    (re.compile(r"(?<![A-Za-z0-9])base(?![A-Za-z0-9])", re.IGNORECASE), "BASE"),
    (
        re.compile(
            r"(?<![A-Za-z0-9])arbitrum(?:\s+one)?(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
        "ARBITRUM",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:optimism|op\s*mainnet)(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
        "OPTIMISM",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])polygon(?![A-Za-z0-9])", re.IGNORECASE),
        "POLYGON",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:tron|trx)(?![A-Za-z0-9])|波场",
            re.IGNORECASE,
        ),
        "TRON",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:solana|sol)(?![A-Za-z0-9])|索拉纳",
            re.IGNORECASE,
        ),
        "SOLANA",
    ),
)
_TOKEN_SYMBOL = r"([A-Za-z][A-Za-z0-9()'`\-]*)"
_NETWORK_QUALIFIER = (
    r"(?:(?:[A-Za-z]+(?:\s+[A-Za-z]+)?|[\u4e00-\u9fff]+)\s*上的)?"
)
_TARGET_SWAP_PATTERNS = (
    re.compile(
        rf"(?:想要?|要)?(?:兑换|换)(?:一点点|一些|一点|点儿|点)\s*"
        rf"{_NETWORK_QUALIFIER}\s*{_TOKEN_SYMBOL}\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:换成|换为|兑换成|兑换为)\s*"
        rf"{_NETWORK_QUALIFIER}\s*{_TOKEN_SYMBOL}\s*$",
        re.IGNORECASE,
    ),
)
_SOURCE_SWAP_PATTERN = re.compile(
    rf"用\s*{_NETWORK_QUALIFIER}\s*{_TOKEN_SYMBOL}(?:\s*来)?\s*(?:兑换|换)",
    re.IGNORECASE,
)
_CHAIN_NAME = (
    r"(?:Ethereum|ETH|ERC20|Base|BSC|BEP20|Arbitrum(?:\s+One)?|Optimism|Polygon|"
    r"以太坊主网|以太坊|以太|币安智能链|币安链)"
)
_SOURCE_CHAIN_ASSET_SWAP_PATTERN = re.compile(
    rf"(?:^|\s)(?:用\s*)?({_CHAIN_NAME})\s*(?:链|网络)?\s*上(?:的)?\s*"
    rf"(?:(?:\d+(?:\.\d*)?|\.\d+)\s*)?{_TOKEN_SYMBOL}(?:\s*来)?\s*"
    rf"(?:兑换|换)(?:成|为)?\s*{_TOKEN_SYMBOL}\s*$",
    re.IGNORECASE,
)
_ALL_SWAP_CHAINS_PATTERN = re.compile(
    rf"都在\s*({_CHAIN_NAME})\s*(?:链|网络)?", re.IGNORECASE
)
_SOURCE_CHAIN_PATTERNS = (
    re.compile(
        rf"用\s*({_CHAIN_NAME})\s*(?:链|网络)?\s*上(?:的)?", re.IGNORECASE
    ),
    re.compile(rf"来源(?:也)?在\s*({_CHAIN_NAME})\s*(?:链|网络)?", re.IGNORECASE),
)
_DESTINATION_CHAIN_PATTERNS = (
    re.compile(
        rf"(?:换|兑换)(?:一点点|一些|一点|点儿|点)\s*({_CHAIN_NAME})\s*"
        rf"(?:链|网络)?\s*上(?:的)?",
        re.IGNORECASE,
    ),
    re.compile(rf"目标(?:也)?在\s*({_CHAIN_NAME})\s*(?:链|网络)?", re.IGNORECASE),
)


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


def unambiguous_amount_with_unit(value: str) -> tuple[str, str | None] | None:
    """Extract a complete numeric amount and its optional token unit.

    Keeping the unit lets callers distinguish ``5 USDC`` (a source amount)
    from ``5 USDT`` (a desired destination amount) instead of silently
    assigning either one to the input amount slot.
    """
    raw = str(value)
    amount_only = _AMOUNT_ONLY.fullmatch(raw)
    if amount_only:
        return amount_only.group(1), None
    with_unit = _AMOUNT_WITH_UNIT.fullmatch(raw)
    if with_unit:
        return with_unit.group(1), canonical_symbol(with_unit.group(2))
    return None


def mentioned_amount_with_unit(value: str) -> tuple[str, str] | None:
    """Extract one explicit token-qualified amount from a natural-language message."""
    matches = list(_AMOUNT_WITH_UNIT_MENTION.finditer(str(value)))
    if len(matches) != 1:
        return None
    match = matches[0]
    return match.group(1), canonical_symbol(match.group(2))


def transaction_query_hints(value: str) -> dict[str, str]:
    """Extract explicit transaction lookup fields without inferring missing values."""
    message = str(value).strip()
    hints: dict[str, str] = {}
    hash_match = _TRANSACTION_HASH_MENTION.search(message)
    if hash_match:
        hints["transaction_hash"] = hash_match.group(0)
    for pattern, chain in _TRANSACTION_CHAIN_MENTIONS:
        if pattern.search(message):
            hints["transaction_chain"] = chain
            break
    return hints


def chain_id_for(value: str) -> int | None:
    return _CHAIN_IDS.get(canonical_chain(value))


def swap_direction_hints(value: str) -> dict[str, str]:
    """Extract only swap directions made explicit by stable Chinese grammar."""
    message = str(value).strip()
    hints: dict[str, str] = {}
    source_chain_asset_match = _SOURCE_CHAIN_ASSET_SWAP_PATTERN.search(message)
    if source_chain_asset_match:
        hints["source_chain"] = canonical_chain(source_chain_asset_match.group(1))
        hints["source_symbol"] = canonical_symbol(source_chain_asset_match.group(2))
        hints["destination_symbol"] = canonical_symbol(source_chain_asset_match.group(3))
    all_chains_match = _ALL_SWAP_CHAINS_PATTERN.search(message)
    if all_chains_match:
        chain = canonical_chain(all_chains_match.group(1))
        hints.update(source_chain=chain, destination_chain=chain)
    else:
        for pattern in _SOURCE_CHAIN_PATTERNS:
            source_chain_match = pattern.search(message)
            if source_chain_match:
                hints["source_chain"] = canonical_chain(source_chain_match.group(1))
                break
        for pattern in _DESTINATION_CHAIN_PATTERNS:
            destination_chain_match = pattern.search(message)
            if destination_chain_match:
                hints["destination_chain"] = canonical_chain(destination_chain_match.group(1))
                break
    source_match = _SOURCE_SWAP_PATTERN.search(message)
    if source_match:
        hints["source_symbol"] = canonical_symbol(source_match.group(1))
    for pattern in _TARGET_SWAP_PATTERNS:
        target_match = pattern.search(message)
        if target_match:
            hints["destination_symbol"] = canonical_symbol(target_match.group(1))
            break
    return hints
