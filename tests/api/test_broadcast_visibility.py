import pytest

from wallet_agent.api.app import _chain_broadcast_status
from wallet_agent.domain.models import TransactionStatus


@pytest.mark.asyncio
async def test_chain_broadcast_status_classifies_receipt_and_pending_transaction():
    class Adapter:
        async def get_transaction_receipt(self, _hash):
            return {"status": "0x1"}

    assert await _chain_broadcast_status(Adapter(), "0x" + "1" * 64) == "confirmed"

    class Pending:
        async def get_transaction_receipt(self, _hash):
            return None

        async def get_transaction(self, _hash):
            return {"hash": _hash}

    assert await _chain_broadcast_status(Pending(), "0x" + "2" * 64) == "broadcast_pending"


@pytest.mark.asyncio
async def test_chain_broadcast_status_distinguishes_missing_and_observer_failure():
    class Missing:
        async def get_transaction_receipt(self, _hash):
            return None

        async def get_transaction(self, _hash):
            return None

    assert await _chain_broadcast_status(Missing(), "0x" + "3" * 64) == "not_propagated"

    class Broken:
        async def get_transaction_receipt(self, _hash):
            raise RuntimeError("rpc down")

    assert await _chain_broadcast_status(Broken(), "0x" + "4" * 64) == "unknown"

    class TransactionObserverBroken:
        async def get_transaction_receipt(self, _hash):
            return None

        async def get_transaction(self, _hash):
            raise RuntimeError("transaction lookup down")

    assert await _chain_broadcast_status(TransactionObserverBroken(), "0x" + "7" * 64) == "unknown"


@pytest.mark.asyncio
async def test_chain_broadcast_status_classifies_sender_nonce_advanced_as_replaced():
    class Replaced:
        async def get_transaction_receipt(self, _hash):
            return None

        async def get_transaction(self, _hash):
            return None

        async def get_transaction_count(self, _sender):
            return 8

    assert (
        await _chain_broadcast_status(
            Replaced(),
            "0x" + "5" * 64,
            sender="0x" + "1" * 40,
            nonce=7,
        )
        == "dropped_or_replaced"
    )


@pytest.mark.asyncio
async def test_chain_broadcast_status_uses_the_status_probe_without_a_receipt():
    """Bitcoin and account-model chains only expose ``get_transaction_status``."""

    class Confirmed:
        async def get_transaction_status(self, _hash):
            return TransactionStatus.CONFIRMED

    class Pending:
        async def get_transaction_status(self, _hash):
            return TransactionStatus.PENDING

    class Inconclusive:
        async def get_transaction_status(self, _hash):
            return TransactionStatus.UNKNOWN

    txid = "a" * 64
    assert await _chain_broadcast_status(Confirmed(), txid) == "confirmed"
    assert await _chain_broadcast_status(Pending(), txid) == "broadcast_pending"
    assert await _chain_broadcast_status(Inconclusive(), txid) == "unknown"


@pytest.mark.asyncio
async def test_chain_broadcast_status_is_unknown_when_nothing_can_observe():
    class Blind:
        async def get_native_balance(self, _address):  # pragma: no cover - unused
            return None

    assert await _chain_broadcast_status(Blind(), "0x" + "9" * 64) == "unknown"
