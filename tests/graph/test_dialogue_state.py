from wallet_agent.graph.nodes import dialogue_snapshot, latest_public_result
from wallet_agent.graph.state import AgentState


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
        "referent": {
            "kind": "wallet_query",
            "id": "wallet_query",
            "facts": {"kind": "wallet_query", "wallet": {"chain": "ETH"}},
        },
        "language": "zh",
        "last_result_kind": "wallet_query",
    }


def test_dialogue_snapshot_projects_price_referent_for_asset_followups():
    state = {
        "request": {"message": "那 ETH 呢？"},
        "conversation_history": [
            {
                "role": "assistant",
                "content": "BTC 当前价格约为 $60000。",
                "artifacts": {
                    "response": {
                        "kind": "price_query",
                        "prices": [
                            {
                                "asset": {"chain": "ETH", "symbol": "BTC"},
                                "usd_price": "60000",
                            }
                        ],
                    }
                },
            }
        ],
    }

    snapshot = dialogue_snapshot(state)

    assert snapshot["active_topic"] == "price"
    assert snapshot["referent"] == {
        "kind": "asset",
        "id": "ETH:BTC",
        "facts": {"chain": "ETH", "symbol": "BTC", "usd_price": "60000"},
    }


def test_dialogue_snapshot_projects_transaction_referent():
    state = {
        "request": {"message": "刚才那笔交易到账了吗？"},
        "conversation_history": [
            {
                "role": "assistant",
                "content": "交易正在确认。",
                "artifacts": {
                    "response": {
                        "kind": "transaction_status",
                        "chain": "BASE",
                        "tx_hash": "0xabc",
                        "status": "pending",
                    }
                },
            }
        ],
    }

    snapshot = dialogue_snapshot(state)

    assert snapshot["referent"] == {
        "kind": "transaction",
        "id": "BASE:0xabc",
        "facts": {"chain": "BASE", "tx_hash": "0xabc", "status": "pending"},
    }


def test_agent_state_declares_read_loop_fields():
    annotations = AgentState.__annotations__

    assert "read_loop_count" in annotations
    assert "read_tool_calls" in annotations
    assert "read_facts" in annotations
