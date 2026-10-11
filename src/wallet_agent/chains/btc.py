"""Bitcoin (Esplora REST) adapter: reads plus unsigned PSBT transfers."""

from __future__ import annotations

import base64
import hashlib
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from wallet_agent.domain.addresses import (
    base58check_payload,
    decode_bech32_segwit,
    valid_bitcoin_address,
)
from wallet_agent.domain.chains import Capability, CapabilitySnapshot
from wallet_agent.domain.models import (
    Asset,
    FeeEstimate,
    TokenBalance,
    TransactionRecord,
    TransactionStatus,
    UnsignedTransaction,
)

from ._common import http_call, normalize_raw_integer, token_balance

PSBT_MAGIC = b"psbt\xff"
DUST_LIMIT = 546
DEFAULT_FEE_RATE_SAT_VB = Decimal("2")
DEFAULT_FEE_TARGET = 6

# BIP174 key types. Signers need the redeem script and the BIP32 derivation of
# every input, and they verify that change belongs to the wallet, so a PSBT
# without them cannot be signed by Bitcoin Core or a hardware wallet.
PSBT_IN_NON_WITNESS_UTXO = 0x00
PSBT_IN_WITNESS_UTXO = 0x01
PSBT_IN_REDEEM_SCRIPT = 0x04
PSBT_IN_BIP32_DERIVATION = 0x06
PSBT_OUT_SCRIPT = 0x00
PSBT_OUT_BIP32_DERIVATION = 0x07

_HARDENED = 0x80000000

# Conservative per-input/output virtual sizes keyed by address family.
_INPUT_VBYTES = {"p2pkh": 148, "p2sh": 91, "p2wpkh": 68, "p2tr": 58}
_OUTPUT_VBYTES = {"p2pkh": 34, "p2sh": 32, "p2wpkh": 31, "p2tr": 43}
_TX_OVERHEAD_VBYTES = 11


def hash160(data: bytes) -> bytes:
    try:
        digest = hashlib.new("ripemd160")
    except ValueError as exc:  # pragma: no cover - depends on the OpenSSL build
        raise RuntimeError("Bitcoin legacy addresses require ripemd160 support") from exc
    digest.update(hashlib.sha256(data).digest())
    return digest.digest()


def address_family(address: str) -> str:
    """Classify a mainnet address as p2pkh, p2sh, p2wpkh, or p2tr."""
    value = address.strip()
    if value.startswith("1"):
        return "p2pkh"
    if value.startswith("3"):
        return "p2sh"
    decoded = decode_bech32_segwit(value)
    if decoded is None:
        raise ValueError(f"invalid Bitcoin address: {address!r}")
    version, program = decoded
    if version == 0 and len(program) == 20:
        return "p2wpkh"
    if version == 1 and len(program) == 32:
        return "p2tr"
    raise ValueError(f"unsupported Bitcoin address type: {address!r}")


def script_pubkey(address: str) -> bytes:
    value = address.strip()
    family = address_family(value)
    if family == "p2pkh":
        payload = base58check_payload(value, version=0)
        assert payload is not None
        return b"\x76\xa9\x14" + payload[1:] + b"\x88\xac"
    if family == "p2sh":
        payload = base58check_payload(value, version=5)
        assert payload is not None
        return b"\xa9\x14" + payload[1:] + b"\x87"
    decoded = decode_bech32_segwit(value)
    assert decoded is not None
    version, program = decoded
    # Witness scripts use OP_0 for v0 and OP_1..OP_16 (0x51..0x60) for later versions.
    opcode = 0x00 if version == 0 else 0x50 + version
    return bytes([opcode, len(program)]) + program


def _varint(value: int) -> bytes:
    if value < 0xFD:
        return bytes([value])
    if value <= 0xFFFF:
        return b"\xfd" + value.to_bytes(2, "little")
    if value <= 0xFFFFFFFF:
        return b"\xfe" + value.to_bytes(4, "little")
    return b"\xff" + value.to_bytes(8, "little")


def _outpoint(txid: str, vout: int) -> bytes:
    return bytes.fromhex(txid)[::-1] + vout.to_bytes(4, "little")


def serialize_unsigned_transaction(
    inputs: Sequence[Mapping[str, Any]], outputs: Sequence[tuple[bytes, int]]
) -> bytes:
    """Serialize a legacy-format unsigned transaction (no witness, empty scriptsigs)."""
    payload = (2).to_bytes(4, "little") + _varint(len(inputs))
    for item in inputs:
        payload += _outpoint(str(item["txid"]), int(item["vout"]))
        payload += b"\x00" + b"\xff\xff\xff\xff"
    payload += _varint(len(outputs))
    for script, amount in outputs:
        payload += amount.to_bytes(8, "little") + _varint(len(script)) + script
    return payload + b"\x00\x00\x00\x00"


def _key_value(key_type: int, key_data: bytes, value: bytes) -> bytes:
    key = bytes([key_type]) + key_data
    return _varint(len(key)) + key + _varint(len(value)) + value


def _hex_bytes(value: Any) -> bytes | None:
    text = str(value or "").strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    if not text or len(text) % 2:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def p2sh_redeem_script(pubkey: bytes) -> bytes:
    """BIP49 redeem script: a P2WPKH program wrapping the compressed pubkey."""
    return b"\x00\x14" + hash160(pubkey)


def bip32_derivation_value(fingerprint: str, path: str) -> bytes:
    """``fingerprint(4) || uint32 LE per index`` as BIP174 encodes it."""
    raw = _hex_bytes(fingerprint)
    if raw is None or len(raw) != 4:
        raise ValueError("master_fingerprint must be 4 bytes of hex")
    value = raw
    for segment in str(path).split("/"):
        index = segment.strip()
        if not index or index in {"m", "M"}:
            continue
        hardened = index[-1] in {"'", "h", "H"}
        number = int(index[:-1] if hardened else index)
        if not 0 <= number < _HARDENED:
            raise ValueError(f"derivation index out of range: {index}")
        value += (number | (_HARDENED if hardened else 0)).to_bytes(4, "little")
    return value


def _derivation_fields(
    info: Mapping[str, Any] | None, key_type: int
) -> list[tuple[int, bytes, bytes]]:
    """BIP32 derivation entry for one input or output, when the App supplied it."""
    data = info or {}
    pubkey = _hex_bytes(data.get("pubkey"))
    path = str(data.get("derivation_path") or "").strip()
    fingerprint = str(data.get("master_fingerprint") or "").strip()
    if pubkey is None or not path or not fingerprint:
        return []
    try:
        value = bip32_derivation_value(fingerprint, path)
    except ValueError:
        return []
    return [(key_type, pubkey, value)]


def build_psbt(
    unsigned_tx: bytes,
    *,
    input_metadata: Sequence[Sequence[tuple[int, bytes, bytes]]],
    output_scripts: Sequence[bytes],
    output_metadata: Sequence[Sequence[tuple[int, bytes, bytes]]] = (),
) -> str:
    """Wrap an unsigned transaction and per-input UTXO metadata into a base64 PSBT."""
    payload = PSBT_MAGIC + _key_value(0, b"", unsigned_tx) + b"\x00"
    for fields in input_metadata:
        for key_type, key_data, value in fields:
            payload += _key_value(key_type, key_data, value)
        payload += b"\x00"
    for index, script in enumerate(output_scripts):
        payload += _key_value(PSBT_OUT_SCRIPT, b"", script)
        extra = output_metadata[index] if index < len(output_metadata) else ()
        for key_type, key_data, value in extra:
            payload += _key_value(key_type, key_data, value)
        payload += b"\x00"
    return base64.b64encode(payload).decode("ascii")


def _witness_utxo(value: int, script: bytes) -> bytes:
    return value.to_bytes(8, "little") + _varint(len(script)) + script


def _matches_p2sh(address: str, pubkey: bytes) -> bool:
    """Whether ``pubkey`` really is the BIP49 key behind this P2SH address."""
    # ``base58check_payload`` keeps the version byte, so drop it before comparing.
    payload = base58check_payload(address, version=5)
    return payload is not None and payload[1:] == hash160(p2sh_redeem_script(pubkey))


def pubkey_matches_address(address: str, family: str, pubkey: bytes) -> bool:
    """Whether the reported pubkey actually controls ``address``.

    Signing metadata is only useful when it is tied to the address being spent,
    so anything that does not match is dropped rather than passed to the signer.
    """
    try:
        script = script_pubkey(address)
    except ValueError:
        return False
    if family == "p2wpkh":
        return script == b"\x00\x14" + hash160(pubkey)
    if family == "p2pkh":
        return script == b"\x76\xa9\x14" + hash160(pubkey) + b"\x88\xac"
    if family == "p2tr":
        internal = pubkey[1:] if len(pubkey) == 33 else pubkey
        return len(internal) == 32 and script == b"\x51\x20" + internal
    if family == "p2sh":
        return _matches_p2sh(address, pubkey)
    return False


class BitcoinChainAdapter:
    #: Provider quotes hand back EVM-shaped payloads; UTXO chains must rebuild them.
    transaction_model = "utxo"

    def __init__(
        self,
        transport: Any = None,
        *,
        http_transport: Any = None,
        http: Any = None,
        chain: str = "BTC",
        chain_id: str | int | None = None,
        native_symbol: str = "BTC",
        native_decimals: int = 8,
        token_assets: list[Asset] | None = None,
        base_path: str = "/api",
        fee_rate_sat_vb: Decimal | float | str | None = None,
        fee_target_blocks: int = DEFAULT_FEE_TARGET,
    ) -> None:
        self.transport = transport or http_transport or http
        if self.transport is None:
            raise ValueError("an injected Bitcoin HTTP transport is required")
        self.chain, self.chain_id = chain.upper(), chain_id
        self.native_asset = Asset(
            chain=self.chain, chain_id=chain_id, symbol=native_symbol, decimals=native_decimals
        )
        self.token_assets = token_assets or []
        self.base_path = base_path.rstrip("/")
        self.fee_rate_sat_vb = (
            Decimal(str(fee_rate_sat_vb)) if fee_rate_sat_vb is not None else None
        )
        self.fee_target_blocks = int(fee_target_blocks)

    @property
    def capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(chain=self.chain, available=frozenset(Capability))

    async def validate_address(self, address: str) -> bool:
        return valid_bitcoin_address(address)

    async def get_native_balance(self, address: str) -> TokenBalance:
        stats = await http_call(self.transport, "get", f"{self.base_path}/address/{address}")
        chain_stats = stats.get("chain_stats") if isinstance(stats, Mapping) else None
        sats = 0
        if isinstance(chain_stats, Mapping):
            sats = int(chain_stats.get("funded_txo_sum", 0)) - int(
                chain_stats.get("spent_txo_sum", 0)
            )
        return token_balance(self.native_asset, max(sats, 0))

    async def get_token_balances(self, address: str) -> list[TokenBalance]:
        """Bitcoin has no token ledger for this adapter; only the native asset is tracked."""
        return []

    async def get_transaction_history(
        self, address: str, *, limit: int = 20
    ) -> list[TransactionRecord]:
        rows = await http_call(
            self.transport, "get", f"{self.base_path}/address/{address}/txs"
        )
        records: list[TransactionRecord] = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, Mapping):
                continue
            status = item.get("status") if isinstance(item.get("status"), Mapping) else {}
            received, sent, spent_from, paid_to = self._movements(item, address)
            net = received - sent
            if net > 0:
                direction, counterparty, amount = "receive", spent_from, net
            elif net < 0:
                direction, counterparty, amount = "send", paid_to, -net
            else:
                direction, counterparty, amount = "unknown", paid_to, 0
            block_time = status.get("block_time")
            occurred_at = (
                datetime.fromtimestamp(int(block_time), tz=timezone.utc)
                if isinstance(block_time, (int, float))
                else None
            )
            records.append(
                TransactionRecord(
                    chain=self.chain,
                    chain_id=self.chain_id,
                    tx_hash=str(item.get("txid", "")),
                    status=(
                        TransactionStatus.CONFIRMED
                        if status.get("confirmed")
                        else TransactionStatus.PENDING
                    ),
                    block_number=status.get("block_height"),
                    from_address=spent_from,
                    to_address=paid_to,
                    value=str(amount) if amount else None,
                    direction=direction,
                    symbol=self.native_asset.symbol,
                    decimals=self.native_asset.decimals,
                    counterparty=counterparty,
                    occurred_at=occurred_at,
                )
            )
        return records[:limit]

    @staticmethod
    def _movements(item: Mapping[str, Any], address: str) -> tuple[int, int, str, str]:
        """(received, sent, first spender, first other payee) for one Esplora tx."""
        received = 0
        sent = 0
        spent_from = ""
        paid_to = ""
        for row in item.get("vout") or []:
            if not isinstance(row, Mapping):
                continue
            value = row.get("value")
            if isinstance(value, (int, float)) and row.get("scriptpubkey_address") == address:
                received += int(value)
            elif not paid_to:
                paid_to = str(row.get("scriptpubkey_address") or "")
        for row in item.get("vin") or []:
            if not isinstance(row, Mapping):
                continue
            previous = row.get("prevout")
            if not isinstance(previous, Mapping):
                continue
            value = previous.get("value")
            owner = str(previous.get("scriptpubkey_address") or "")
            if not spent_from and owner:
                spent_from = owner
            if owner == address and isinstance(value, (int, float)):
                sent += int(value)
        return received, sent, spent_from, paid_to

    async def estimate_fee(
        self,
        *,
        to: str | None = None,
        data: str | None = None,
        from_address: str | None = None,
        value: str | None = None,
        vsize: int | None = None,
    ) -> FeeEstimate:
        """Fee for one input and two outputs at the configured mempool target."""
        rate = await self._fee_rate_sat_vb()
        size = vsize or self._estimate_vsize(
            [from_address or to or ""], [to or "", from_address or to or ""]
        )
        sats = int(math.ceil(Decimal(size) * rate))
        return FeeEstimate(
            chain=self.chain,
            chain_id=self.chain_id,
            asset=self.native_asset,
            amount=Decimal(sats) / (Decimal(10) ** self.native_asset.decimals),
            amount_raw=str(sats),
        )

    async def get_transaction_status(self, tx_hash: str) -> TransactionStatus:
        status = await http_call(self.transport, "get", f"{self.base_path}/tx/{tx_hash}/status")
        if not isinstance(status, Mapping):
            return TransactionStatus.UNKNOWN
        return (
            TransactionStatus.CONFIRMED if status.get("confirmed") else TransactionStatus.PENDING
        )

    async def build_native_transfer(
        self,
        *,
        from_address: str,
        to_address: str,
        amount_raw: str,
        from_addresses: Sequence[str] | None = None,
        signing_info: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> UnsignedTransaction:
        """Build an unsigned PSBT paying ``to_address``.

        ``from_addresses`` lists every address the App declared for this wallet
        (receiving and change addresses included); their UTXOs are pooled so a
        transfer is no longer capped at the balance of a single address. Change
        returns to ``from_address``, which also labels the transaction.

        ``signing_info`` maps an address to the ``pubkey`` / ``derivation_path`` /
        ``master_fingerprint`` the App reported for it. Without it a nested SegWit
        input has no redeem script and no signer can produce a signature.
        """
        if not valid_bitcoin_address(from_address) or not valid_bitcoin_address(to_address):
            raise ValueError("invalid Bitcoin address")
        sources = self._spendable_addresses(from_address, from_addresses)
        amount = int(normalize_raw_integer(amount_raw))
        if amount <= 0:
            raise ValueError("transfer amount must be positive")
        utxos = await self._address_utxos(sources)
        if not utxos:
            raise ValueError("no spendable Bitcoin UTXOs for the sender addresses")
        rate = await self._fee_rate_sat_vb()
        selected, fee, change = self._select(utxos, amount, rate, to_address, from_address)
        if change < DUST_LIMIT:
            change = 0
        outputs: list[tuple[bytes, int]] = [(script_pubkey(to_address), amount)]
        if change:
            outputs.append((script_pubkey(from_address), change))
        unsigned_tx = serialize_unsigned_transaction(selected, outputs)
        metadata = await self._input_metadata(selected, signing_info)
        output_metadata: list[list[tuple[int, bytes, bytes]]] = [[] for _ in outputs]
        if change and pubkey_matches_address(
            from_address,
            address_family(from_address),
            _hex_bytes((signing_info or {}).get(from_address, {}).get("pubkey")) or b"",
        ):
            # A signer verifies that the change output belongs to this wallet.
            output_metadata[1] = _derivation_fields(
                (signing_info or {}).get(from_address), PSBT_OUT_BIP32_DERIVATION
            )
        psbt = build_psbt(
            unsigned_tx,
            input_metadata=metadata,
            output_scripts=[script for script, _ in outputs],
            output_metadata=output_metadata,
        )
        return UnsignedTransaction(
            chain=self.chain,
            chain_id=self.chain_id,
            to=to_address,
            data=psbt,
            value=str(amount),
            display={
                "action": "native_transfer",
                "format": "psbt",
                "from": from_address,
                # ``display`` is a string map; Bitcoin addresses never contain a comma.
                "from_addresses": ",".join(sources),
                "to": to_address,
                "amount_raw": str(amount),
                "fee_raw": str(fee),
                "fee_rate_sat_vb": str(rate),
                "change_raw": str(change),
                "input_count": str(len(selected)),
                "unsigned_tx_hex": unsigned_tx.hex(),
            },
        )

    async def _fee_rate_sat_vb(self) -> Decimal:
        if self.fee_rate_sat_vb is not None:
            return self.fee_rate_sat_vb
        try:
            estimates = await http_call(
                self.transport, "get", f"{self.base_path}/fee-estimates"
            )
        except Exception:
            return DEFAULT_FEE_RATE_SAT_VB
        if not isinstance(estimates, Mapping) or not estimates:
            return DEFAULT_FEE_RATE_SAT_VB
        for target in (str(self.fee_target_blocks), *sorted(estimates, key=lambda item: int(item))):
            raw = estimates.get(target)
            try:
                rate = Decimal(str(raw))
            except Exception:
                continue
            if rate > 0:
                return rate
        return DEFAULT_FEE_RATE_SAT_VB

    async def _utxos(self, address: str) -> list[dict[str, Any]]:
        rows = await http_call(self.transport, "get", f"{self.base_path}/address/{address}/utxo")
        utxos = [
            {"txid": str(row["txid"]), "vout": int(row["vout"]), "value": int(row["value"])}
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, Mapping) and "txid" in row and "vout" in row
        ]
        return sorted(utxos, key=lambda item: item["value"], reverse=True)

    @staticmethod
    def _spendable_addresses(
        from_address: str, from_addresses: Sequence[str] | None
    ) -> list[str]:
        """Ordered, de-duplicated spendable addresses with the primary first."""
        ordered: list[str] = []
        for candidate in (from_address, *(from_addresses or ())):
            address = str(candidate or "").strip()
            if address and address not in ordered and valid_bitcoin_address(address):
                ordered.append(address)
        return ordered

    async def _address_utxos(self, addresses: Sequence[str]) -> list[dict[str, Any]]:
        """Tag every UTXO with the address it belongs to, largest value first."""
        utxos: list[dict[str, Any]] = []
        for address in addresses:
            for item in await self._utxos(address):
                utxos.append({**item, "address": address})
        return sorted(utxos, key=lambda item: item["value"], reverse=True)

    @staticmethod
    def _estimate_vsize(inputs: Sequence[str], outputs: Sequence[str]) -> int:
        total = _TX_OVERHEAD_VBYTES
        for address in inputs:
            total += _INPUT_VBYTES.get(address_family(address) if address else "p2wpkh", 68)
        for address in outputs:
            if not address:
                continue
            total += _OUTPUT_VBYTES.get(address_family(address), 31)
        return total

    def _select(
        self,
        utxos: Sequence[Mapping[str, Any]],
        amount: int,
        rate: Decimal,
        to_address: str,
        change_address: str,
    ) -> tuple[list[dict[str, Any]], int, int]:
        selected: list[dict[str, Any]] = []
        total = 0
        for utxo in utxos:
            selected.append(dict(utxo))
            total += int(utxo["value"])
            fee, change = self._settle(selected, total, amount, rate, to_address, change_address)
            if total >= amount + fee:
                return selected, fee, change
        raise ValueError("insufficient Bitcoin balance for the amount and network fee")

    def _settle(
        self,
        inputs: Sequence[Mapping[str, Any]],
        total: int,
        amount: int,
        rate: Decimal,
        to_address: str,
        change_address: str,
    ) -> tuple[int, int]:
        """Pick the fee/change pair for the current selection; drop dust change into the fee."""
        with_change = self._fee_for(inputs, rate, [to_address, change_address])
        change = total - amount - with_change
        if change >= DUST_LIMIT:
            return with_change, change
        without_change = self._fee_for(inputs, rate, [to_address])
        if total - amount - without_change >= 0:
            return without_change, 0
        return with_change, 0

    def _fee_for(
        self,
        inputs: Sequence[Mapping[str, Any]],
        rate: Decimal,
        outputs: Sequence[str],
    ) -> int:
        size = _TX_OVERHEAD_VBYTES
        for item in inputs:
            family = address_family(str(item.get("address") or ""))
            size += _INPUT_VBYTES.get(family, _INPUT_VBYTES["p2wpkh"])
        size += sum(_OUTPUT_VBYTES.get(address_family(item), 31) for item in outputs if item)
        return int(math.ceil(Decimal(size) * rate))

    async def _input_metadata(
        self,
        inputs: Sequence[Mapping[str, Any]],
        signing_info: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[list[tuple[int, bytes, bytes]]]:
        signing = signing_info or {}
        metadata: list[list[tuple[int, bytes, bytes]]] = []
        for item in inputs:
            address = str(item.get("address") or "")
            family = address_family(address)
            script = script_pubkey(address)
            fields: list[tuple[int, bytes, bytes]] = []
            info = signing.get(address)
            pubkey = _hex_bytes((info or {}).get("pubkey"))
            matched = pubkey is not None and pubkey_matches_address(address, family, pubkey)
            if not matched:
                pubkey = None
            if family in {"p2wpkh", "p2tr"}:
                fields.append(
                    (PSBT_IN_WITNESS_UTXO, b"", _witness_utxo(int(item["value"]), script))
                )
            else:
                redeem = (
                    p2sh_redeem_script(pubkey)
                    if family == "p2sh" and pubkey is not None
                    else None
                )
                if redeem is not None:
                    # Nested SegWit is a witness spend, so the signer needs the
                    # witness UTXO plus the redeem script, not the previous
                    # transaction hex.
                    fields.append(
                        (PSBT_IN_WITNESS_UTXO, b"", _witness_utxo(int(item["value"]), script))
                    )
                    fields.append((PSBT_IN_REDEEM_SCRIPT, b"", redeem))
                else:
                    previous = await http_call(
                        self.transport,
                        "get",
                        f"{self.base_path}/tx/{item['txid']}/hex",
                        text=True,
                    )
                    if not isinstance(previous, str) or not previous:
                        raise ValueError(
                            f"cannot fetch the previous transaction for UTXO {item['txid']}"
                        )
                    fields.append((PSBT_IN_NON_WITNESS_UTXO, b"", bytes.fromhex(previous)))
            fields.extend(_derivation_fields(info, PSBT_IN_BIP32_DERIVATION) if matched else [])
            metadata.append(fields)
        return metadata


BitcoinAdapter = BitcoinChainAdapter
BTCAdapter = BitcoinChainAdapter
