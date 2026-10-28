"""App-pinned language drives card copy, not just message detection."""

from wallet_agent.graph.nodes import (
    _requested_language,
    _status_language,
    _wallet_summary_message,
)


def test_requested_language_prefers_top_level_language():
    state = {"request": {"message": "兑换", "language": "en"}}
    assert _requested_language(state) == "en"
    assert _status_language(state) == "en"
    assert _wallet_summary_message({}, _status_language(state)).startswith(
        "I couldn't find"
    )


def test_requested_language_supports_metadata_language():
    state = {"request": {"message": "swap", "metadata": {"language": "zh"}}}
    assert _requested_language(state) == "zh"
    assert _status_language(state) == "zh"
    assert _wallet_summary_message({}, _status_language(state)).startswith(
        "当前钱包没有查到"
    )


def test_language_falls_back_to_message_detection():
    state = {"request": {"message": "兑换"}}
    assert _requested_language(state) is None
    assert _status_language(state) == "zh"

    english = {"request": {"message": "what is my balance"}}
    assert _status_language(english) == "en"
