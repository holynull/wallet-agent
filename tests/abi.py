"""ABI encoding helpers used to rebuild Bridgers' TRON swap calldata."""

import pytest

from wallet_agent.abi import (
    abi_encode_arguments,
    encode_swap_calldata,
    function_selector,
)


def test_swapeth_selector_is_stable():
    """keccak256("swapEth(string,string,uint256)")[:4] — the signature Bridgers
    sends for TRON swaps."""
    assert function_selector("swapEth(string,string,uint256)") == "16b3b4c2"
    assert function_selector("transfer(address,uint256)") == "a9059cbb"


def test_encode_swap_calldata_matches_bridgers_tron_payload():
    """The exact parameter list from a real Bridgers TRX -> BTC swap must encode
    with dynamic head offsets for the two strings and a static uint tail."""
    calldata = encode_swap_calldata(
        "swapEth(string,string,uint256)",
        [
            {"type": "string", "value": "BTC|1dcve2|0.01|bridgers|0"},
            {"type": "string", "value": "1Nqs6wvbYfZK15GQ43ydsiDU7NDnnWR2tm"},
            {"type": "uint256", "value": "0x4844"},
        ],
    )
    assert calldata.startswith("0x16b3b4c2")
    # string heads point to 0x60 and 0xa0; the uint is inline.
    assert calldata[10 : 10 + 64] == "0" * 62 + "60"
    assert calldata[74 : 74 + 64] == "0" * 62 + "a0"


def test_encode_swap_calldata_decodes_tron_base58_address_and_offsets():
    """A Bridgers TRON-source swap returns the token address in Base58Check form
    (``TR7...``) as the first, static argument. It must decode to the 20-byte
    hex address, and the string heads that follow must keep correct offsets
    (a static arg before a dynamic one previously broke the offset math)."""
    calldata = encode_swap_calldata(
        "swap(address,string,string,uint256,uint256)",
        [
            {"type": "address", "value": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
            {"type": "string", "value": "BNB(BSC)|1dcve2|0.01|bridgers|0"},
            {"type": "string", "value": "0x13c68429D80F56E72C02122652852a1DAfA60AE0"},
            {"type": "uint256", "value": "0x989680"},
            {"type": "uint256", "value": "0x2fa9c7ea4e8000"},
        ],
    )
    assert calldata.startswith("0x9ddf93bb")  # swap(address,string,string,uint256,uint256)
    # USDT(TRON) decodes from Base58Check to 0xa614f803b6fd780986a42c78ec9c7f77e6ded13c.
    assert calldata[10 : 10 + 64] == "0" * 24 + "a614f803b6fd780986a42c78ec9c7f77e6ded13c"
    # String heads point at 0xa0 and 0xe0 (they must follow the inline address,
    # not be indexed by a stale dynamic counter).
    assert calldata[74 : 74 + 64] == "0" * 62 + "a0"
    assert calldata[138 : 138 + 64] == "0" * 62 + "e0"
    # The uints are inline at slots 3 and 4.
    assert calldata[202 : 202 + 64] == "0" * 58 + "989680"
    assert calldata[266 : 266 + 64] == "0" * 50 + "2fa9c7ea4e8000"


def test_encode_swap_calldata_keeps_evm_address_inline():
    """EVM-style hex addresses (0x + 40) must encode unchanged, not be treated
    as TRON Base58Check."""
    calldata = encode_swap_calldata(
        "transfer(address,uint256)",
        [
            {"type": "address", "value": "0x13c68429D80F56E72C02122652852a1DAfA60AE0"},
            {"type": "uint256", "value": "1000"},
        ],
    )
    assert calldata.startswith("0xa9059cbb")
    assert calldata[10 : 10 + 64] == "0" * 24 + "13c68429D80F56E72C02122652852a1DAfA60AE0".lower()


@pytest.mark.parametrize(
    ("arg_type", "value", "expected"),
    [
        ("uint256", "0x4844", "0" * 60 + "4844"),
        ("uint256", 184, "0" * 62 + "b8"),
        ("bool", "true", "0" * 63 + "1"),
        ("address", "0x" + "1" * 40, "0" * 24 + "1" * 40),
    ],
)
def test_abi_encode_static_arguments(arg_type, value, expected):
    assert abi_encode_arguments([arg_type], [value]) == expected


def test_abi_encode_requires_matching_lengths():
    with pytest.raises(ValueError, match="same length"):
        abi_encode_arguments(["uint256", "uint256"], [1])


def test_abi_encode_rejects_unsupported_types():
    with pytest.raises(ValueError, match="unsupported"):
        abi_encode_arguments(["tuple"], [(1, 2)])


def test_abi_encode_rejects_oversized_fixed_bytes():
    with pytest.raises(ValueError, match="longer than"):
        abi_encode_arguments(["bytes2"], ["0xdeadbeef"])


def test_encode_swap_calldata_rejects_parameter_without_type():
    with pytest.raises(ValueError, match="missing a type"):
        encode_swap_calldata("swapEth(string,string,uint256)", [{"value": "abc"}])
