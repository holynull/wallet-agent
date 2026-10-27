"""Explicit lifecycle routing rules (regression for V169-IOS-025-era flows)."""

from wallet_agent.graph.routes import route_after_intent


def test_fresh_swap_prepare_is_not_hijacked_by_stale_broadcast():
    """A new swap_prepare with its own swap_request/selected_quote must route to
    the prepare path even when an older broadcast_tx_hash is still in the
    checkpoint. Previously it went to register_broadcast -> status_poll, which
    failed on the old order and left the App without a card."""
    state = {
        "intent": "swap_prepare",
        "broadcast_tx_hash": "0x" + "a" * 64,
        "fresh_swap": True,
        "swap_request": {"source_asset": {"symbol": "USDC"}},
        "selected_quote": {"provider_reference": "quote-1"},
    }

    assert route_after_intent(state) == "swap_resolve_prepare"


def test_swap_status_after_broadcast_still_registers_the_order():
    state = {"intent": "swap_status", "broadcast_tx_hash": "0x" + "b" * 64}
    assert route_after_intent(state) == "register_broadcast"


def test_swap_prepare_without_fresh_quote_still_registers_old_broadcast():
    state = {"intent": "swap_prepare", "broadcast_tx_hash": "0x" + "c" * 64}
    assert route_after_intent(state) == "register_broadcast"
