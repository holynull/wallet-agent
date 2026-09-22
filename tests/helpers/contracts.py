"""Small, value-safe helpers for API and provider contract tests."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from wallet_agent.domain.normalization import canonical_chain, canonical_symbol

_ADDRESS = re.compile(r"^(?:0[xX][0-9a-fA-F]{32,}|[1-9A-HJ-NP-Za-km-z]{32,})$")
_SENSITIVE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret|"
    r"private[_-]?key|seed|mnemonic|token|signature)",
    re.IGNORECASE,
)


def _fail(field: str, detail: str) -> None:
    raise AssertionError(f"{field}: {detail}")


def assert_response_shape(
    payload: object,
    *,
    required_keys: tuple[str, ...] = (),
    list_keys: tuple[str, ...] = (),
) -> None:
    if not isinstance(payload, Mapping):
        _fail("payload", f"type={type(payload).__name__}")
    for key in required_keys:
        if key not in payload:
            _fail(key, "missing")
    for key in list_keys:
        if key not in payload:
            _fail(key, "missing")
        if not isinstance(payload[key], list):
            _fail(key, f"type={type(payload[key]).__name__}")


def assert_decimal_in_range(value: object, *, field: str) -> int:
    if value is None or isinstance(value, bool):
        _fail(field, "type=invalid")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        _fail(field, "type=invalid")
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        _fail(field, "type=invalid")
    result = int(decimal)
    if not 0 <= result <= 255:
        _fail(field, "range=invalid")
    return result


def assert_raw_matches_human(
    amount: object,
    raw_amount: object,
    decimals: int,
    *,
    field: str = "amount",
) -> None:
    decimal_count = assert_decimal_in_range(decimals, field="decimals")
    try:
        human = Decimal(str(amount))
        raw = Decimal(str(raw_amount))
    except (InvalidOperation, ValueError, TypeError):
        _fail(field, "type=invalid")
    if not human.is_finite() or not raw.is_finite():
        _fail(field, "type=non-finite")
    if human * (Decimal(10) ** decimal_count) != raw:
        _fail(field, "value=inconsistent")


def assert_asset_identity(
    asset: Mapping[str, object],
    *,
    chain: str,
    symbol: str,
    address: str | None = None,
) -> None:
    if not isinstance(asset, Mapping):
        _fail("asset", f"type={type(asset).__name__}")
    actual_chain = asset.get("chain")
    actual_symbol = asset.get("symbol")
    if actual_chain is None or canonical_chain(str(actual_chain)) != canonical_chain(chain):
        _fail("chain", "value=inconsistent")
    if actual_symbol is None or canonical_symbol(str(actual_symbol)) != canonical_symbol(symbol):
        _fail("symbol", "value=inconsistent")
    if address is not None:
        actual_address = asset.get("address")
        if actual_address is None or str(actual_address).strip().lower() != address.strip().lower():
            _fail("address", "value=inconsistent")


def assert_provider_metadata(
    value: Mapping[str, object],
    *,
    provider: str,
    required_keys: tuple[str, ...] = (),
) -> None:
    if not isinstance(value, Mapping):
        _fail("provider", f"type={type(value).__name__}")
    actual = value.get("provider")
    if actual is None or str(actual).strip().casefold() != provider.strip().casefold():
        _fail("provider", "value=inconsistent")
    for key in required_keys:
        if key not in value or value[key] is None:
            _fail(key, "missing")


def redact_fixture(value: object) -> object:
    """Return a recursively redacted fixture copy without exposing secrets."""
    if isinstance(value, Mapping):
        result: dict[object, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE.search(key_text):
                result[key] = "<redacted>"
            else:
                result[key] = redact_fixture(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_fixture(item) for item in value]
    if isinstance(value, str) and _ADDRESS.match(value.strip()):
        return "<redacted-address>"
    return value


def safe_shape(value: object) -> dict[str, object]:
    """Describe only keys, type names, and list lengths for safe diagnostics."""
    if not isinstance(value, Mapping):
        return {"keys": [], "types": {"value": type(value).__name__}, "list_lengths": {}}
    keys = sorted(str(key) for key in value)
    types = {str(key): type(value[key]).__name__ for key in sorted(value, key=str)}
    list_lengths = {
        str(key): len(value[key])
        for key in sorted(value, key=str)
        if isinstance(value[key], list)
    }
    return {"keys": keys, "types": types, "list_lengths": list_lengths}
