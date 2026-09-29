from wallet_agent.graph.nodes import dialogue_snapshot, latest_public_result


def test_latest_public_result_returns_the_latest_user_visible_card():
    history = [
        {
            "role": "assistant",
            "content": "余额",
            "artifacts": {"response": {"kind": "wallet_query"}},
        },
        {"role": "user", "content": "哪几项？"},
        {
            "role": "assistant",
            "content": "报价",
            "artifacts": {"response": {"kind": "swap_quote", "quotes": [{"provider": "x"}]}},
        },
    ]

    result = latest_public_result(history)

    assert result == {"kind": "swap_quote", "quotes": [{"provider": "x"}]}


def test_dialogue_snapshot_tracks_topic_referent_and_language():
    state = {
        "request": {"message": "哪 2 项？"},
        "conversation_history": [
            {"role": "user", "content": "我的余额"},
            {
                "role": "assistant",
                "content": "ETH 和 USDC",
                "artifacts": {
                    "response": {
                        "kind": "wallet_query",
                        "wallet": {"chain": "ETH"},
                    }
                },
            },
        ],
    }

    assert dialogue_snapshot(state) == {
        "goal": "read",
        "active_topic": "wallet",
        "referent": {"kind": "wallet_query", "wallet": {"chain": "ETH"}},
        "language": "zh",
        "last_result_kind": "wallet_query",
    }
