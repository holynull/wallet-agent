"""Transfer draft normalization across chain families.

Regression: asking for a BTC transfer while the wallet's active network is an
EVM chain must resolve the BTC chain and the BTC sender from ``wallet_accounts``
instead of falling back to the EVM context address.
"""

from wallet_agent.graph.nodes import _transfer_draft_request

EVM_ADDRESS = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"
BTC_SEGWIT = "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5"
BTC_TAPROOT = "bc1p47u7v2k5cpfqt0gxftkat3ade7ucs588g8lr0znzr9le06taty6q6x9gut"
TRON_ADDRESS = "TNwXHkvCwj3nayWPbFbxD7rBPncBsyBS27"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

MULTICHAIN_CONTEXT = {
    "address": EVM_ADDRESS,
    "chain": "ETH",
    "supported_networks": ["ETH", "BASE", "BTC", "TRON"],
    "wallet_accounts": [
        {"chain": "ETH", "address": EVM_ADDRESS},
        {"chain": "BASE", "address": EVM_ADDRESS},
        {"chain": "BTC", "address": BTC_SEGWIT},
        {"chain": "BTC", "address": BTC_TAPROOT},
        {"chain": "TRON", "address": TRON_ADDRESS},
    ],
}


def test_btc_transfer_infers_chain_without_explicit_chain():
    """The model omits chain; the EVM active network must not hijack BTC."""
    request, missing = _transfer_draft_request(
        {"symbol": "BTC", "amount": "0.00001", "recipient": BTC_SEGWIT},
        MULTICHAIN_CONTEXT,
    )
    assert missing == []
    assert request is not None
    assert request.chain == "BTC"
    assert request.sender == BTC_SEGWIT
    assert request.recipient == BTC_SEGWIT
    assert request.amount_raw == "1000"
    assert request.token is None


def test_btc_transfer_uses_btc_sender_when_chain_given():
    """With chain=BTC the sender must come from the BTC account, not the EVM one."""
    request, missing = _transfer_draft_request(
        {
            "symbol": "BTC",
            "amount": "0.00001",
            "recipient": BTC_TAPROOT,
            "chain": "BTC",
            "decimals": 8,
        },
        MULTICHAIN_CONTEXT,
    )
    assert missing == []
    assert request is not None
    assert request.chain == "BTC"
    assert request.sender == BTC_SEGWIT
    assert request.recipient == BTC_TAPROOT
    assert request.amount_raw == "1000"


def test_btc_transfer_without_btc_account_reports_missing_sender():
    context = {"address": EVM_ADDRESS, "chain": "ETH"}
    request, missing = _transfer_draft_request(
        {"symbol": "BTC", "amount": "0.00001", "recipient": BTC_SEGWIT},
        context,
    )
    assert request is None
    assert missing == ["transfer_sender"]


def test_explicit_foreign_family_sender_is_replaced():
    """The ReAct tool seeds the connected EVM account as sender; on BTC it must be ignored."""
    request, missing = _transfer_draft_request(
        {
            "symbol": "BTC",
            "amount": "0.00001",
            "recipient": BTC_TAPROOT,
            "chain": "BTC",
            "sender": EVM_ADDRESS,
        },
        MULTICHAIN_CONTEXT,
    )
    assert missing == []
    assert request is not None
    assert request.chain == "BTC"
    assert request.sender == BTC_SEGWIT


def test_explicit_sender_is_preserved():
    request, missing = _transfer_draft_request(
        {
            "symbol": "BTC",
            "amount": "0.00001",
            "recipient": BTC_TAPROOT,
            "chain": "BTC",
            "sender": BTC_TAPROOT,
        },
        MULTICHAIN_CONTEXT,
    )
    assert missing == []
    assert request is not None
    assert request.sender == BTC_TAPROOT


def test_evm_native_transfer_unchanged():
    request, missing = _transfer_draft_request(
        {"symbol": "ETH", "amount": "0.01", "recipient": "0x" + "1" * 40},
        MULTICHAIN_CONTEXT,
    )
    assert missing == []
    assert request is not None
    assert request.chain == "ETH"
    assert request.sender == EVM_ADDRESS
    assert request.amount_raw == "10000000000000000"
    assert request.token is None


def test_erc20_transfer_unchanged():
    request, missing = _transfer_draft_request(
        {
            "symbol": "USDC",
            "amount": "1",
            "recipient": "0x" + "1" * 40,
            "chain": "BASE",
            "token_address": USDC_BASE,
            "decimals": 6,
        },
        MULTICHAIN_CONTEXT,
    )
    assert missing == []
    assert request is not None
    assert request.chain == "BASE"
    assert request.sender == EVM_ADDRESS
    assert request.amount_raw == "1000000"
    assert request.token is not None
    assert request.token.address == USDC_BASE
