import json
from decimal import Decimal

import pytest

from tests.helpers.contracts import (
    assert_asset_identity,
    assert_decimal_in_range,
    assert_provider_metadata,
    assert_raw_matches_human,
    assert_response_shape,
    redact_fixture,
    safe_shape,
)


def test_raw_amount_requires_exact_decimal_conversion():
    assert_raw_matches_human("12.5", "12500000", 6)
    with pytest.raises(AssertionError, match="amount"):
        assert_raw_matches_human("12.5", "1250000", 6)


def test_missing_or_invalid_decimals_are_rejected():
    assert assert_decimal_in_range("6", field="decimals") == 6
    for value in (None, "NaN", 256, True):
        with pytest.raises(AssertionError, match="decimals"):
            assert_decimal_in_range(value, field="decimals")


def test_safe_shape_does_not_return_addresses_or_secrets():
    shape = safe_shape({"address": "0x" + "a" * 40, "apiKey": "secret", "items": [1]})
    assert shape == {
        "keys": ["address", "apiKey", "items"],
        "types": {"address": "str", "apiKey": "str", "items": "list"},
        "list_lengths": {"items": 1},
    }
    assert "secret" not in json.dumps(shape)


def test_response_shape_checks_required_and_list_keys():
    assert_response_shape(
        {"items": [], "status": "ok"}, required_keys=("status",), list_keys=("items",)
    )
    with pytest.raises(AssertionError, match="status"):
        assert_response_shape({}, required_keys=("status",))
    with pytest.raises(AssertionError, match="items"):
        assert_response_shape({"items": "not-a-list"}, list_keys=("items",))


def test_asset_identity_normalizes_chain_symbol_and_address():
    assert_asset_identity(
        {"chain": "ethereum", "symbol": "USDT(ERC20)", "address": "0xabc"},
        chain="ETH",
        symbol="usdt",
        address="0xabc",
    )
    with pytest.raises(AssertionError, match="chain"):
        assert_asset_identity({"chain": "BSC", "symbol": "USDT"}, chain="ETH", symbol="USDT")
    with pytest.raises(AssertionError, match="address"):
        assert_asset_identity(
            {"chain": "ETH", "symbol": "USDT", "address": "0x1"},
            chain="ETH",
            symbol="USDT",
            address="0x2",
        )


def test_provider_metadata_requires_provider_and_safe_keys():
    assert_provider_metadata(
        {"provider": "OmniBridge", "provider_reference": "q-1"},
        provider="omnibridge",
        required_keys=("provider_reference",),
    )
    with pytest.raises(AssertionError, match="provider_reference"):
        assert_provider_metadata(
            {"provider": "omnibridge"},
            provider="omnibridge",
            required_keys=("provider_reference",),
        )


def test_redact_fixture_replaces_sensitive_and_address_like_values():
    redacted = redact_fixture(
        {"address": "0x" + "a" * 40, "api_key": "secret", "nested": ["token"]}
    )
    assert redacted["address"] == "<redacted-address>"
    assert redacted["api_key"] == "<redacted>"
    assert redacted["nested"] == ["token"]


def test_redact_fixture_redacts_bare_token_signature_and_uppercase_addresses():
    redacted = redact_fixture(
        {"token": "bearer", "signature": "signed", "address": "0X" + "A" * 40}
    )
    assert redacted == {
        "token": "<redacted>",
        "signature": "<redacted>",
        "address": "<redacted-address>",
    }


def test_raw_amount_rejects_non_finite_values():
    with pytest.raises(AssertionError, match="amount"):
        assert_raw_matches_human(Decimal("NaN"), "1", 0)
