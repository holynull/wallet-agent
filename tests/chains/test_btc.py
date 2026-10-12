import base64

import pytest

from wallet_agent.chains.btc import (
    DUST_LIMIT,
    PSBT_IN_BIP32_DERIVATION,
    PSBT_IN_REDEEM_SCRIPT,
    PSBT_MAGIC,
    PSBT_OUT_BIP32_DERIVATION,
    BitcoinChainAdapter,
    _key_value,
    address_family,
    bip32_derivation_value,
    hash160,
    script_pubkey,
)
from wallet_agent.domain.models import TransactionStatus

P2PKH = "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"
P2SH = "3FGALr2UNebKnBTErhePUk3CuzmDd9FJux"
P2WPKH = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"
P2TR = "bc1p47u7v2k5cpfqt0gxftkat3ade7ucs588g8lr0znzr9le06taty6q6x9gut"

PUBKEY = "0279BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798"
FINGERPRINT = "a1b2c3d4"
DERIVATION = "49'/0'/0'/0/0"


def _nested_segwit_address(pubkey: bytes) -> str:
    """Independent Base58Check encoding of the BIP49 address for ``pubkey``."""
    import hashlib

    redeem = b"\x00\x14" + hashlib.new("ripemd160", hashlib.sha256(pubkey).digest()).digest()
    payload = b"\x05" + hashlib.new("ripemd160", hashlib.sha256(redeem).digest()).digest()
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(payload + checksum, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    return encoded


NESTED_SEGWIT = _nested_segwit_address(bytes.fromhex(PUBKEY))

UTXOS = [
    {"txid": "aa" * 32, "vout": 0, "value": 200_000, "status": {"confirmed": True}},
    {"txid": "bb" * 32, "vout": 1, "value": 50_000, "status": {"confirmed": True}},
]
PREV_TX_HEX = "0100000001" + "cc" * 32 + "0000000000ffffffff01"


class EsploraFake:
    """Minimal Esplora stub that records the paths it was asked for."""

    def __init__(self, utxos=None, fee_estimates=None):
        self.utxos = UTXOS if utxos is None else utxos
        self.fee_estimates = {"1": 21.4, "6": 3.0} if fee_estimates is None else fee_estimates
        self.paths: list[str] = []

    async def get(self, path, **kwargs):
        self.paths.append(path)
        if path.endswith("/utxo"):
            return self.utxos
        if path.endswith("/fee-estimates"):
            return self.fee_estimates
        if path.endswith("/hex"):
            return PREV_TX_HEX
        if path.endswith("/status"):
            return {"confirmed": True, "block_height": 800_000}
        if path.endswith("/txs"):
            return [
                {"txid": "dd" * 32, "status": {"confirmed": True, "block_height": 799_999}},
                {"txid": "ee" * 32, "status": {"confirmed": False}},
            ]
        if "/address/" in path:
            return {"chain_stats": {"funded_txo_sum": 250_000, "spent_txo_sum": 40_000}}
        return {}


def test_address_families_and_scripts():
    assert address_family(P2PKH) == "p2pkh"
    assert address_family(P2SH) == "p2sh"
    assert address_family(P2WPKH) == "p2wpkh"
    assert address_family(P2TR) == "p2tr"
    assert script_pubkey(P2PKH).hex() == "76a9147680adec8eabcabac676be9e83854ade0bd22cdb88ac"
    assert script_pubkey(P2SH).hex() == "a91494de63c752573d6f8ec8e9f926629534a29f29e787"
    assert script_pubkey(P2WPKH).hex() == "0014683b711cda50e044eff663f74cd078546a21f984"
    assert (
        script_pubkey(P2TR).hex()
        == "5120afb9e62ad4c05205bd064aedd5c7adcfb98850e741fe378a62197f97e97d5934"
    )
    with pytest.raises(ValueError):
        address_family("not-a-bitcoin-address")


async def test_validate_and_read_only_methods():
    adapter = BitcoinChainAdapter(EsploraFake())

    assert await adapter.validate_address(P2WPKH) is True
    assert await adapter.validate_address("0x" + "1" * 40) is False
    assert (await adapter.get_native_balance(P2WPKH)).amount_raw == "210000"
    assert await adapter.get_token_balances(P2WPKH) == []

    history = await adapter.get_transaction_history(P2WPKH)
    assert [item.status for item in history] == [
        TransactionStatus.CONFIRMED,
        TransactionStatus.PENDING,
    ]
    assert await adapter.get_transaction_status("dd" * 32) == TransactionStatus.CONFIRMED


async def test_estimate_fee_uses_the_mempool_target():
    adapter = BitcoinChainAdapter(EsploraFake())

    fee = await adapter.estimate_fee(from_address=P2WPKH, to=P2TR)

    assert fee.amount_raw == "459"  # 153 vB at the 6-block rate of 3 sat/vB
    assert adapter.capabilities.supports(next(iter(adapter.capabilities.available)))


async def test_build_native_transfer_returns_a_witness_psbt():
    transport = EsploraFake()
    adapter = BitcoinChainAdapter(transport)

    tx = await adapter.build_native_transfer(
        from_address=P2WPKH, to_address=P2TR, amount_raw="120000"
    )

    raw = base64.b64decode(tx.data)
    assert raw.startswith(PSBT_MAGIC)
    assert tx.chain == "BTC"
    assert tx.to == P2TR
    assert tx.value == "120000"
    assert tx.display["action"] == "native_transfer"
    assert tx.display["format"] == "psbt"
    assert tx.display["fee_raw"] == "459"
    assert tx.display["change_raw"] == "79541"
    assert tx.display["input_count"] == "1"
    assert tx.display["unsigned_tx_hex"].startswith("0200000001" + "aa" * 32)
    assert not any(path.endswith("/hex") for path in transport.paths)


async def test_build_native_transfer_attaches_prev_tx_for_legacy_inputs():
    transport = EsploraFake()
    adapter = BitcoinChainAdapter(transport)

    tx = await adapter.build_native_transfer(
        from_address=P2SH, to_address=P2TR, amount_raw="120000"
    )

    assert tx.display["fee_raw"] == "531"
    assert any(path.endswith(f"/tx/{'aa' * 32}/hex") for path in transport.paths)
    assert base64.b64decode(tx.data).startswith(PSBT_MAGIC)


async def test_build_native_transfer_signs_nested_segwit_with_reported_material():
    """A BIP49 input needs the redeem script; a signer needs the BIP32 path."""
    transport = EsploraFake()
    adapter = BitcoinChainAdapter(transport)
    signing = {
        NESTED_SEGWIT: {
            "pubkey": PUBKEY,
            "derivation_path": DERIVATION,
            "master_fingerprint": FINGERPRINT,
        }
    }

    tx = await adapter.build_native_transfer(
        from_address=NESTED_SEGWIT,
        to_address=P2TR,
        amount_raw="120000",
        signing_info=signing,
    )

    blob = base64.b64decode(tx.data)
    key = bytes.fromhex(PUBKEY)
    derivation = bip32_derivation_value(FINGERPRINT, DERIVATION)
    assert _key_value(PSBT_IN_REDEEM_SCRIPT, b"", b"\x00\x14" + hash160(key)) in blob
    assert _key_value(PSBT_IN_BIP32_DERIVATION, key, derivation) in blob
    # The change output proves it belongs to this wallet.
    assert _key_value(PSBT_OUT_BIP32_DERIVATION, key, derivation) in blob
    # Nested SegWit is a witness spend: no previous-transaction fetch is needed.
    assert not any(path.endswith("/hex") for path in transport.paths)


async def test_build_native_transfer_drops_signing_material_for_another_address():
    transport = EsploraFake()
    adapter = BitcoinChainAdapter(transport)

    tx = await adapter.build_native_transfer(
        from_address=P2SH,
        to_address=P2TR,
        amount_raw="120000",
        signing_info={
            P2SH: {
                "pubkey": PUBKEY,
                "derivation_path": DERIVATION,
                "master_fingerprint": FINGERPRINT,
            }
        },
    )

    blob = base64.b64decode(tx.data)
    assert bytes.fromhex(PUBKEY) not in blob
    # With no verified key it falls back to the previous-transaction hex.
    assert any(path.endswith("/hex") for path in transport.paths)


async def test_unconfirmed_outputs_are_not_spendable_by_default():
    utxos = [{"txid": "ee" * 32, "vout": 0, "value": 200_000, "status": {"confirmed": False}}]
    adapter = BitcoinChainAdapter(EsploraFake(utxos=utxos))

    with pytest.raises(ValueError, match="no spendable Bitcoin UTXOs"):
        await adapter.build_native_transfer(
            from_address=P2WPKH, to_address=P2TR, amount_raw="120000"
        )

    permissive = BitcoinChainAdapter(EsploraFake(utxos=utxos), min_confirmations=0)
    tx = await permissive.build_native_transfer(
        from_address=P2WPKH, to_address=P2TR, amount_raw="120000"
    )
    assert tx.display["format"] == "psbt"


async def test_rbf_is_opt_in_and_visible_in_the_display():
    adapter = BitcoinChainAdapter(EsploraFake())
    locked = await adapter.build_native_transfer(
        from_address=P2WPKH, to_address=P2TR, amount_raw="120000"
    )
    assert locked.display["rbf"] == "false"
    assert "ffffffff" in locked.display["unsigned_tx_hex"]  # nSequence opts out

    bumpable = BitcoinChainAdapter(EsploraFake(), rbf_enabled=True)
    replaceable = await bumpable.build_native_transfer(
        from_address=P2WPKH, to_address=P2TR, amount_raw="120000"
    )
    assert replaceable.display["rbf"] == "true"
    assert "fdffffff" in replaceable.display["unsigned_tx_hex"]


async def test_build_native_transfer_selects_extra_inputs_and_rejects_dust_balance():
    adapter = BitcoinChainAdapter(EsploraFake())
    tx = await adapter.build_native_transfer(
        from_address=P2WPKH, to_address=P2TR, amount_raw=str(240_000)
    )
    assert tx.display["input_count"] == "2"
    assert tx.display["change_raw"] == "9337"

    small = BitcoinChainAdapter(EsploraFake(utxos=UTXOS[:1]))
    with pytest.raises(ValueError):
        await small.build_native_transfer(
            from_address=P2WPKH, to_address=P2TR, amount_raw=str(300_000)
        )


async def test_build_native_transfer_requires_spendable_utxos_and_valid_addresses():
    adapter = BitcoinChainAdapter(EsploraFake(utxos=[]))
    with pytest.raises(ValueError):
        await adapter.build_native_transfer(
            from_address=P2WPKH, to_address=P2TR, amount_raw="1000"
        )
    with pytest.raises(ValueError):
        await BitcoinChainAdapter(EsploraFake()).build_native_transfer(
            from_address=P2WPKH, to_address=P2PKH.replace("1", "9", 1), amount_raw="1000"
        )


def test_dust_limit_is_the_bitcoin_standard_value():
    assert DUST_LIMIT == 546


class PerAddressEsplora:
    """Esplora stub that serves a different UTXO set per address."""

    def __init__(self, utxos_by_address, fee_estimates=None):
        self.utxos_by_address = utxos_by_address
        self.fee_estimates = {"6": 3.0} if fee_estimates is None else fee_estimates
        self.paths: list[str] = []

    async def get(self, path, **kwargs):
        self.paths.append(path)
        if path.endswith("/utxo"):
            for address, utxos in self.utxos_by_address.items():
                if path == f"/api/address/{address}/utxo":
                    return utxos
            return []
        if path.endswith("/fee-estimates"):
            return self.fee_estimates
        if path.endswith("/hex"):
            return PREV_TX_HEX
        return {}


async def test_build_native_transfer_pools_utxos_across_addresses():
    transport = PerAddressEsplora(
        {
            P2WPKH: [{"txid": "aa" * 32, "vout": 0, "value": 60_000}],
            P2TR: [{"txid": "bb" * 32, "vout": 1, "value": 70_000}],
        }
    )
    adapter = BitcoinChainAdapter(transport)

    tx = await adapter.build_native_transfer(
        from_address=P2WPKH,
        to_address=P2TR,
        amount_raw="120000",
        from_addresses=[P2WPKH, P2TR],
    )

    # 11 overhead + p2tr 58 + p2wpkh 68 + p2tr out 43 + p2wpkh change 31 = 211 vB
    assert tx.display["fee_raw"] == str(211 * 3)
    assert tx.display["change_raw"] == str(130_000 - 120_000 - 211 * 3)
    assert tx.display["input_count"] == "2"
    assert tx.display["from_addresses"] == f"{P2WPKH},{P2TR}"
    # Both addresses' UTXOs are spent, so both must be queried.
    assert f"/api/address/{P2WPKH}/utxo" in transport.paths
    assert f"/api/address/{P2TR}/utxo" in transport.paths
    unsigned = tx.display["unsigned_tx_hex"]
    assert "aa" * 32 in unsigned and "bb" * 32 in unsigned
    assert script_pubkey(P2WPKH).hex() in unsigned  # change returns to the primary
    assert script_pubkey(P2TR).hex() in unsigned
    assert not any(path.endswith("/hex") for path in transport.paths)


async def test_build_native_transfer_is_not_capped_by_the_primary_address():
    """The primary address alone cannot cover the amount, but the wallet can."""
    transport = PerAddressEsplora(
        {
            P2WPKH: [{"txid": "aa" * 32, "vout": 0, "value": 1_000}],
            P2TR: [{"txid": "bb" * 32, "vout": 1, "value": 200_000}],
        }
    )
    adapter = BitcoinChainAdapter(transport)

    tx = await adapter.build_native_transfer(
        from_address=P2WPKH,
        to_address=P2TR,
        amount_raw="120000",
        from_addresses=[P2WPKH, P2TR],
    )
    assert tx.display["input_count"] == "1"

    capped = BitcoinChainAdapter(transport)
    with pytest.raises(ValueError):
        await capped.build_native_transfer(
            from_address=P2WPKH, to_address=P2TR, amount_raw="120000"
        )


async def test_build_native_transfer_attaches_prev_tx_only_for_legacy_inputs():
    transport = PerAddressEsplora(
        {
            P2SH: [{"txid": "cc" * 32, "vout": 0, "value": 100_000}],
            P2WPKH: [{"txid": "aa" * 32, "vout": 0, "value": 50_000}],
        }
    )
    adapter = BitcoinChainAdapter(transport)

    tx = await adapter.build_native_transfer(
        from_address=P2SH,
        to_address=P2TR,
        amount_raw="120000",
        from_addresses=[P2SH, P2WPKH],
    )

    # 11 + p2sh 91 + p2wpkh 68 + p2tr out 43 + p2sh change 32 = 245 vB
    assert tx.display["fee_raw"] == str(245 * 3)
    assert tx.display["input_count"] == "2"
    assert [path for path in transport.paths if path.endswith("/hex")] == [
        f"/api/tx/{'cc' * 32}/hex"
    ]


async def test_transaction_history_derives_direction_amount_and_counterparty():
    class HistoryEsplora:
        async def get(self, path, **_kwargs):
            if path.endswith("/txs"):
                return [
                    {
                        "txid": "aa" * 32,
                        "status": {
                            "confirmed": True,
                            "block_height": 900_000,
                            "block_time": 1_750_000_000,
                        },
                        "vin": [{"prevout": {"scriptpubkey_address": P2WPKH, "value": 100_000}}],
                        "vout": [
                            {"scriptpubkey_address": P2TR, "value": 99_000},
                            {"scriptpubkey_address": P2WPKH, "value": 500},
                        ],
                    },
                    {
                        "txid": "bb" * 32,
                        "status": {
                            "confirmed": True,
                            "block_height": 900_001,
                            "block_time": 1_750_001_000,
                        },
                        "vin": [{"prevout": {"scriptpubkey_address": P2TR, "value": 40_000}}],
                        "vout": [{"scriptpubkey_address": P2WPKH, "value": 40_000}],
                    },
                ]
            return {}

    records = await BitcoinChainAdapter(HistoryEsplora()).get_transaction_history(P2WPKH)
    outgoing, incoming = records

    assert outgoing.direction == "send"
    assert outgoing.value == "99500"  # 100_000 spent, 500 returned as change
    assert outgoing.counterparty == P2TR
    assert outgoing.symbol == "BTC" and outgoing.decimals == 8
    assert outgoing.occurred_at is not None

    assert incoming.direction == "receive"
    assert incoming.value == "40000"
    assert incoming.counterparty == P2TR
