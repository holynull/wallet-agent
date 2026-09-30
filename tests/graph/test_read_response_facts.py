from wallet_agent.graph.facts import validate_response_draft


def test_rejects_price_not_present_in_facts():
    draft = {
        "language": "en",
        "message": "ETH is $2,000.",
        "suggestions": [],
    }

    assert validate_response_draft(
        draft,
        {"prices": [{"symbol": "ETH", "usd_price": "1800"}]},
    ) is None


def test_rejects_pending_transaction_rewritten_as_confirmed():
    draft = {
        "language": "zh",
        "message": "交易已确认到账。",
        "suggestions": [],
    }

    assert validate_response_draft(
        draft,
        {"transaction": {"status": "pending", "message": "交易正在确认。"}},
    ) is None


def test_rejects_suggestion_amount_above_verified_balance():
    draft = {
        "language": "en",
        "message": "You can swap 20 USDC.",
        "suggestions": [
            {
                "label": "Swap",
                "message": "Swap 20 USDC",
                "data": {"intent": "swap_quote", "input_amount": "20", "source_symbol": "USDC"},
            }
        ],
    }

    assert validate_response_draft(
        draft,
        {"balances": [{"symbol": "USDC", "amount": "12.5"}]},
    ) is None


def test_accepts_draft_that_only_repeats_verified_facts():
    draft = {
        "language": "en",
        "message": "ETH is $1,800.",
        "suggestions": [],
    }

    result = validate_response_draft(
        draft,
        {"prices": [{"symbol": "ETH", "usd_price": "1800"}]},
    )

    assert result == draft
