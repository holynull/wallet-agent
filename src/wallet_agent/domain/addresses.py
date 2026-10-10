"""Chain-aware public address validation shared by models, adapters, and reads."""

from __future__ import annotations

import hashlib
import re

from .normalization import canonical_chain

EVM_NETWORKS = ("ETH", "BSC", "BASE", "POLYGON", "ARBITRUM", "OPTIMISM")
EVM_ALIASES = frozenset((*EVM_NETWORKS, "EVM"))
SUPPORTED_FAMILIES = frozenset((*EVM_ALIASES, "BTC", "TRON", "SOLANA"))

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {char: index for index, char in enumerate(BASE58_ALPHABET)}
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BECH32_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GENERATORS = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
_MAX_WITNESS_VERSION = 16


def base58_decode(value: str) -> bytes:
    """Decode Base58, preserving leading-zero bytes as leading '1' characters."""
    number = 0
    for char in value:
        number = number * 58 + _BASE58_INDEX[char]
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + raw


def base58check_payload(value: str, *, version: int | None = None) -> bytes | None:
    """Return the 21-byte Base58Check payload, or None when the address is malformed."""
    try:
        raw = base58_decode(value)
    except (KeyError, ValueError):
        return None
    if len(raw) != 25:
        return None
    if hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] != raw[-4:]:
        return None
    if version is not None and raw[0] != version:
        return None
    return raw[:-4]


def valid_evm_address(address: str) -> bool:
    return bool(_EVM_ADDRESS.fullmatch(address.strip()))


def decode_bech32_segwit(address: str) -> tuple[int, bytes] | None:
    """Return ``(witness_version, witness_program)`` for a mainnet bech32/bech32m address."""
    value = address.strip()
    if not value:
        return None
    if value != value.lower() and value != value.upper():
        return None
    lowered = value.lower()
    if not lowered.startswith("bc1") or len(lowered) > 90:
        return None
    try:
        data = [_BECH32_ALPHABET.index(char) for char in lowered[3:]]
    except ValueError:
        return None
    if len(data) < 7 or data[0] > _MAX_WITNESS_VERSION:
        return None
    checksum = 1
    for part in [3, 3, 0, 2, 3, *data]:  # HRP expansion of 'bc'
        high = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ part
        for index, generator in enumerate(_BECH32_GENERATORS):
            if (high >> index) & 1:
                checksum ^= generator
    if checksum != (1 if data[0] == 0 else 0x2BC830A3):
        return None
    bits = 0
    accumulator = 0
    program: list[int] = []
    for part in data[1:-6]:
        accumulator = (accumulator << 5) | part
        bits += 5
        if bits >= 8:
            bits -= 8
            program.append((accumulator >> bits) & 255)
    if bits >= 5 or (accumulator << (8 - bits)) & 255:
        return None
    if not 2 <= len(program) <= 40:
        return None
    if data[0] == 0 and len(program) not in (20, 32):
        return None
    return data[0], bytes(program)


def valid_bitcoin_address(address: str) -> bool:
    """Bitcoin mainnet Base58Check (P2PKH/P2SH) and BIP173/BIP350 witness addresses."""
    value = address.strip()
    if not value:
        return False
    if value.startswith(("1", "3")):
        return base58check_payload(value, version=0 if value[0] == "1" else 5) is not None
    return decode_bech32_segwit(value) is not None


def valid_tron_address(address: str) -> bool:
    return base58check_payload(address.strip(), version=0x41) is not None


def valid_solana_address(address: str) -> bool:
    try:
        return len(base58_decode(address.strip())) == 32
    except (KeyError, ValueError):
        return False


def is_valid_address(chain: str, address: str) -> bool:
    """Validate an address with the rules of its chain family.

    Unknown chains keep the original EVM-only contract so existing callers and
    eval fixtures behave exactly as before.
    """
    if not isinstance(address, str):
        return False
    normalized = canonical_chain(chain)
    if normalized == "BTC" or normalized == "BITCOIN":
        return valid_bitcoin_address(address)
    if normalized == "TRON":
        return valid_tron_address(address)
    if normalized == "SOLANA":
        return valid_solana_address(address)
    return valid_evm_address(address)


def validate_address(chain: str, address: str) -> str:
    if not is_valid_address(chain, address):
        raise ValueError(f"invalid {canonical_chain(chain)} address: {address!r}")
    return address.strip()
