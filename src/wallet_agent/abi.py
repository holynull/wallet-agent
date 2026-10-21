"""Minimal EVM ABI encoding: function selectors and argument encoding.

Providers (Bridgers) return TRON swap payloads as a function signature plus a
list of ``{type, value}`` parameters instead of a ready ``0x`` calldata. The
wallet must sign a node-built transaction, and the node's
``/wallet/triggersmartcontract`` needs the real calldata (selector + ABI
encoded arguments). These helpers build it without pulling in a full ``web3``
stack.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from eth_hash.auto import keccak as _keccak  # type: ignore[import-untyped]

from wallet_agent.domain.addresses import base58check_payload, valid_evm_address

_WORD = 32


def keccak256(data: bytes) -> bytes:
    """Ethereum ``keccak256`` (not NIST SHA3-256)."""
    return _keccak(data)


def function_selector(signature: str) -> str:
    """First four bytes of ``keccak256(signature)`` as an 8-hex-digit string."""
    return keccak256(signature.encode("utf-8")).hex()[:8]


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return 0
    return int(text, 16) if text.lower().startswith("0x") else int(text, 10)


def _pad_right(data: bytes) -> bytes:
    """Pad bytes to a whole 32-byte word (used for ``string``/``bytes`` tails)."""
    remainder = len(data) % _WORD
    return data + (b"\x00" * (_WORD - remainder)) if remainder else data


def _is_dynamic(arg_type: str) -> bool:
    if arg_type in {"string", "bytes"}:
        return True
    if arg_type.endswith("[]"):
        return True
    return False


def _encode_static(arg_type: str, value: Any) -> bytes:
    """Encode a static ABI value as a 32-byte word."""
    if arg_type.startswith("uint"):
        bits = int(arg_type[4:] or "256")
        if not (0 < bits <= 256 and bits % 8 == 0):
            raise ValueError(f"unsupported uint size: {arg_type}")
        return _as_int(value).to_bytes(32, "big")
    if arg_type.startswith("int"):
        bits = int(arg_type[3:] or "256")
        if not (0 < bits <= 256 and bits % 8 == 0):
            raise ValueError(f"unsupported int size: {arg_type}")
        return _as_int(value).to_bytes(32, "big", signed=True)
    if arg_type == "address":
        raw = str(value).strip()
        if valid_evm_address(raw):
            return bytes.fromhex(raw[2:]).rjust(32, b"\x00")
        # Bridgers sends TRON token/router addresses in Base58Check form
        # (``T...``) for TRON-source swaps; the contract expects the 20-byte
        # address (0x41 type byte stripped).
        payload = base58check_payload(raw, version=0x41)
        if payload is not None and len(payload) == 21:
            return payload[1:].rjust(32, b"\x00")
        # Legacy fallback: treat the text as raw hex (EVM without the 0x prefix).
        hexpart = raw[2:] if raw.lower().startswith("0x") else raw
        return bytes.fromhex(hexpart.zfill(40)).rjust(32, b"\x00")
    if arg_type == "bool":
        return (1 if str(value).lower() in {"true", "1"} else 0).to_bytes(32, "big")
    if arg_type.startswith("bytes") and arg_type[5:].isdigit():
        size = int(arg_type[5:])
        if not 1 <= size <= 32:
            raise ValueError(f"unsupported bytes size: {arg_type}")
        raw = bytes.fromhex(str(value).strip().removeprefix("0x"))
        if len(raw) > size:
            raise ValueError(f"{arg_type} value longer than {size} bytes")
        return raw.ljust(32, b"\x00")
    raise ValueError(f"unsupported static ABI type: {arg_type}")


def _encode_tail(arg_type: str, value: Any) -> bytes:
    """Encode the tail (offset-target payload) of a dynamic ABI value."""
    if arg_type == "string":
        data = str(value).encode("utf-8")
        return len(data).to_bytes(32, "big") + _pad_right(data)
    if arg_type == "bytes":
        data = bytes.fromhex(str(value).strip().removeprefix("0x"))
        return len(data).to_bytes(32, "big") + _pad_right(data)
    if arg_type.endswith("[]"):
        base = arg_type[: -len("[]")]
        if _is_dynamic(base):
            raise ValueError(f"unsupported dynamic array of dynamic type: {arg_type}")
        items = list(value)
        body = b"".join(_encode_static(base, item) for item in items)
        return len(items).to_bytes(32, "big") + body
    raise ValueError(f"unsupported dynamic ABI type: {arg_type}")


def abi_encode_arguments(
    arg_types: Sequence[str], values: Sequence[Any]
) -> str:
    """Encode a list of (type, value) pairs into ``0x``-free ABI calldata."""
    if len(arg_types) != len(values):
        raise ValueError("ABI types and values must have the same length")
    # Keep each argument's static head and (optional) dynamic tail aligned by
    # argument position: dynamic tails are indexed by the argument slot, not by
    # a separate counter, or offsets break whenever a static type (e.g. an
    # ``address``) precedes a dynamic one.
    slots: list[tuple[bytes | None, bytes | None]] = []
    for arg_type, value in zip(arg_types, values):
        if _is_dynamic(arg_type):
            slots.append((None, _encode_tail(arg_type, value)))
        else:
            slots.append((_encode_static(arg_type, value), None))
    head_size = _WORD * len(arg_types)
    cursor = head_size
    encoded_head = bytearray()
    tails: list[bytes] = []
    for head, tail in slots:
        if head is None:
            encoded_head += cursor.to_bytes(32, "big")
            cursor += len(tail)
            tails.append(tail)
        else:
            encoded_head += head
    return (bytes(encoded_head) + b"".join(tails)).hex()


def encode_swap_calldata(
    function_name: str, parameters: Sequence[Mapping[str, Any]]
) -> str:
    """Build ``0x`` calldata from a function signature and parameter list.

    ``parameters`` mirrors Bridgers' TRON ``txData.parameter`` shape: a list of
    ``{"type": "string", "value": ...}`` entries. The returned hex is the full
    ``selector + encoded-arguments`` calldata a TRON ``triggersmartcontract``
    call expects.
    """
    arg_types = [str(item.get("type", "")).strip() for item in parameters]
    values = [item.get("value") for item in parameters]
    if any(not arg_type for arg_type in arg_types):
        raise ValueError("Bridgers swap parameter is missing a type")
    return "0x" + function_selector(function_name) + abi_encode_arguments(arg_types, values)
