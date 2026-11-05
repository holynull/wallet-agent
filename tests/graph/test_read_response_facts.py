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


def test_rejects_portfolio_asset_count_that_does_not_match_facts():
    facts = {
        "asset_count": 7,
        "balances": [{"symbol": "BNB", "amount": "0"}],
    }
    for message in ("当前资产组合包含 0 项资产。", "当前资产组合包含 1 项资产。"):
        assert validate_response_draft(
            {"language": "zh", "message": message, "suggestions": []}, facts
        ) is None


def test_transaction_detail_message_summarizes_net_wallet_token_changes():
    from wallet_agent.graph.nodes import _transaction_status_detail_message

    wallet = "0x" + "1" * 40
    response = {
        "kind": "transaction_status",
        "chain": "ETH",
        "status": "confirmed",
        "amount": "2",
        "symbol": "USDC",
        "detail": {
            "chain": "ETH",
            "status": "confirmed",
            "from_address": wallet,
            "to_address": "0x" + "2" * 40,
            "fee": "0.000045666962631906",
            "symbol": "ETH",
            "confirmed_at": "2026-09-12T16:11:23Z",
            "token_transfers": [
                {
                    "symbol": "USDC",
                    "amount": "2",
                    "from_address": wallet,
                    "to_address": "0x" + "3" * 40,
                },
                {
                    "symbol": "NVDAon",
                    "amount": "0.009060991681527839",
                    "from_address": "0x" + "3" * 40,
                    "to_address": wallet,
                },
            ],
        },
    }

    message = _transaction_status_detail_message(
        response, "zh", wallet_address=wallet
    )

    assert "ETH" in message
    assert "2 USDC" in message
    assert "0.009060991681527839 NVDAon" in message
    assert "0.000045666962631906 ETH" in message
