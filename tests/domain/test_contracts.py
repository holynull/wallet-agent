import asyncio
from typing import get_type_hints

import pytest

from wallet_agent.domain.chains import Capability, CapabilityRegistry, CapabilitySnapshot
from wallet_agent.domain.errors import ChainCapabilityUnavailable
from wallet_agent.domain.models import FeeEstimate, TransactionRecord, TransactionStatus
from wallet_agent.domain.providers import ChainAdapter, SwapProvider


def test_unknown_capability_is_structured():
    with pytest.raises(ChainCapabilityUnavailable) as exc_info:
        raise ChainCapabilityUnavailable(chain="WAVES", capability="balances")

    assert exc_info.value.code == "CHAIN_CAPABILITY_UNAVAILABLE"
    assert exc_info.value.to_agent_error().model_dump() == {
        "code": "CHAIN_CAPABILITY_UNAVAILABLE",
        "message": "Chain WAVES does not support balances.",
        "retryable": False,
        "details": {"chain": "WAVES", "capability": "balances"},
    }


def test_capability_registry_returns_typed_snapshot_and_rejects_unknown_operation():
    registry = CapabilityRegistry(
        [CapabilitySnapshot(chain="EVM", available=frozenset({Capability.BALANCES}))]
    )

    snapshot = registry.get("EVM")
    assert snapshot.supports(Capability.BALANCES)
    assert not snapshot.supports(Capability.HISTORY)

    with pytest.raises(ChainCapabilityUnavailable):
        registry.require("EVM", Capability.HISTORY)


def test_provider_and_chain_contract_methods_are_async():
    provider_methods = ("list_assets", "quote", "prepare", "register_broadcast", "get_status")
    chain_methods = (
        "validate_address",
        "get_native_balance",
        "get_token_balances",
        "get_transaction_history",
        "estimate_fee",
        "get_transaction_status",
    )

    assert all(
        asyncio.iscoroutinefunction(getattr(SwapProvider, method)) for method in provider_methods
    )
    assert all(
        asyncio.iscoroutinefunction(getattr(ChainAdapter, method)) for method in chain_methods
    )


def test_contracts_use_concrete_protocol_return_annotations():
    provider_hints = get_type_hints(SwapProvider.quote)
    chain_hints = get_type_hints(ChainAdapter.get_token_balances)
    history_hints = get_type_hints(ChainAdapter.get_transaction_history)
    fee_hints = get_type_hints(ChainAdapter.estimate_fee)
    status_hints = get_type_hints(ChainAdapter.get_transaction_status)

    assert provider_hints["return"].__name__ == "NormalizedQuote"
    assert chain_hints["return"].__origin__ is list
    assert history_hints["return"] == list[TransactionRecord]
    assert fee_hints["return"] is FeeEstimate
    assert status_hints["return"] is TransactionStatus


def test_transaction_status_is_finite():
    assert TransactionStatus.CONFIRMED.value == "confirmed"
    with pytest.raises(ValueError):
        TransactionStatus("arbitrary-provider-status")
