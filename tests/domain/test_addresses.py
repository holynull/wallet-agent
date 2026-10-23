import pytest

from wallet_agent.domain.addresses import (
    is_valid_address,
    normalize_evm_address,
    valid_bitcoin_address,
    valid_evm_address,
    valid_tron_address,
    validate_address,
)
from wallet_agent.domain.chains import native_decimals

BTC_SEGWIT = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"
BTC_TAPROOT = "bc1p47u7v2k5cpfqt0gxftkat3ade7ucs588g8lr0znzr9le06taty6q6x9gut"
BTC_NESTED_SEGWIT = "3FGALr2UNebKnBTErhePUk3CuzmDd9FJux"
BTC_LEGACY = "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"
TRON_ADDRESS = "TNwXHkvCwj3nayWPbFbxD7rBPncBsyBS27"
SOLANA_ADDRESS = "11111111111111111111111111111111"
EVM_ADDRESS = "0x" + "1" * 40


@pytest.mark.parametrize(
    "address",
    [BTC_SEGWIT, BTC_TAPROOT, BTC_NESTED_SEGWIT, BTC_LEGACY],
)
def test_bitcoin_mainnet_addresses_are_valid(address):
    assert valid_bitcoin_address(address) is True
    assert is_valid_address("BTC", address) is True
    assert is_valid_address("bitcoin", address) is True


@pytest.mark.parametrize(
    "address",
    [
        "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d6",
        "1BoatSLRHtKNngkdXEeobR76b53LETtpyS",
        "tb1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5",
        "",
    ],
)
def test_invalid_bitcoin_addresses_are_rejected(address):
    assert valid_bitcoin_address(address) is False


def test_tron_mainnet_address_is_valid():
    assert valid_tron_address(TRON_ADDRESS) is True
    assert is_valid_address("TRON", TRON_ADDRESS) is True
    assert is_valid_address("TRON", TRON_ADDRESS[:-1] + "8") is False


def test_address_families_do_not_cross_validate():
    assert is_valid_address("TRON", BTC_SEGWIT) is False
    assert is_valid_address("BTC", TRON_ADDRESS) is False
    assert is_valid_address("ETH", TRON_ADDRESS) is False
    assert is_valid_address("BTC", EVM_ADDRESS) is False


def test_unknown_chains_keep_the_evm_contract():
    assert is_valid_address("EVM", EVM_ADDRESS) is True
    assert is_valid_address("ARB", EVM_ADDRESS) is True
    assert is_valid_address("SOMECHAIN", "not-an-address") is False
    assert is_valid_address("SOLANA", SOLANA_ADDRESS) is True


def test_evm_ox_prefix_typo_is_normalized_and_valid():
    """A user-typed leading letter O ("Ox3F84…") is a typo for "0x" and must
    not block an otherwise complete EVM transfer from being prepared."""
    mistyped = "Ox" + "3" * 40
    canonical = "0x" + "3" * 40
    assert normalize_evm_address(mistyped) == canonical
    assert normalize_evm_address("ox" + "4" * 40) == "0x" + "4" * 40
    assert normalize_evm_address(canonical) == canonical
    assert valid_evm_address(mistyped) is True
    assert is_valid_address("ETH", mistyped) is True
    assert validate_address("ETH", mistyped) == canonical


def test_validate_address_raises_for_the_wrong_family():
    assert validate_address("BTC", BTC_SEGWIT) == BTC_SEGWIT
    with pytest.raises(ValueError):
        validate_address("BTC", TRON_ADDRESS)


def test_native_decimals_match_each_chain():
    assert native_decimals("ETH") == 18
    assert native_decimals("BSC") == 18
    assert native_decimals("BTC") == 8
    assert native_decimals("TRON") == 6
    assert native_decimals("SOLANA") == 9
    assert native_decimals("BITCOIN") == 8
    assert native_decimals("UNKNOWN") == 18
