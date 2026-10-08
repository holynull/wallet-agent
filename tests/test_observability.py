import logging

from wallet_agent.observability import wallet_event


def test_wallet_event_logs_task_metadata_without_sensitive_request_data(caplog):
    address = "0x" + "1" * 40
    with caplog.at_level(logging.INFO, logger="wallet_agent.events"):
        wallet_event(
            "intent",
            "success",
            duration_ms=12.5,
            state={
                "active_task": {"kind": "swap", "revision": 3},
                "predicted_intent": "swap_quote",
                "route": "swap_quote",
                "wallet_context": {"address": address},
                "request": {"prompt": "secret", "api_key": "secret"},
                "authorization": "secret",
            },
            error_code=None,
        )

    assert '"node":"intent"' in caplog.text
    assert '"task_kind":"swap"' in caplog.text
    assert '"task_revision":3' in caplog.text
    assert '"duration_ms":12.5' in caplog.text
    assert "0x1111...1111" in caplog.text
    assert address not in caplog.text
    assert "api_key" not in caplog.text
    assert "authorization" not in caplog.text
    assert "prompt" not in caplog.text
    assert "request" not in caplog.text


def test_wallet_event_logs_read_only_tool_metadata(caplog):
    with caplog.at_level(logging.INFO, logger="wallet_agent.events"):
        wallet_event(
            "read_only_tool",
            "success",
            duration_ms=4,
            state={"conversation_id": "conversation-1", "read_loop_count": 1},
            tool_name="price_lookup",
            tool_status="completed",
        )

    assert '"conversation_id":"conversation-1"' in caplog.text
    assert '"read_loop_count":1' in caplog.text
    assert '"read_tool_name":"price_lookup"' in caplog.text
    assert '"read_tool_status":"completed"' in caplog.text


def test_wallet_event_logs_safe_portfolio_consistency_metadata(caplog):
    with caplog.at_level(logging.INFO, logger="wallet_agent.events"):
        wallet_event(
            "read_only_observe",
            "success",
            duration_ms=6,
            state={"conversation_id": "conversation-2"},
            query_scope="all",
            resolved_chain_count=2,
            asset_count=7,
            response_validation="fallback",
        )

    assert '"query_scope":"all"' in caplog.text
    assert '"resolved_chain_count":2' in caplog.text
    assert '"asset_count":7' in caplog.text
    assert '"response_validation":"fallback"' in caplog.text
