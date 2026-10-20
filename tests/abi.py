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
