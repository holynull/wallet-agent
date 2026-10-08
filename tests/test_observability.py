import logging

from wallet_agent.observability import MetadataCache, RuntimeMetrics, wallet_event


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


def test_runtime_metrics_aggregates_model_and_tool_timings_and_can_reset():
    metrics = RuntimeMetrics()
    metrics.record_model("deepseek-flash", role="react_agent", elapsed_ms=10)
    metrics.record_model("deepseek-flash", role="react_agent", elapsed_ms=20, status="error")
    metrics.record_tool(
        "asset_discovery",
        elapsed_ms=7,
        status="success",
        cache_status="hit",
    )
    metrics.record_tool(
        "asset_discovery",
        elapsed_ms=13,
        status="error",
        error_code="ASSET_DISCOVERY_FAILED",
        cache_status="miss",
    )

    snapshot = metrics.snapshot()

    assert snapshot["model_calls"] == 2
    assert snapshot["tool_calls"] == 2
    assert snapshot["models"]["deepseek-flash"]["count"] == 2
    assert snapshot["models"]["deepseek-flash"]["error_count"] == 1
    assert snapshot["tools"]["asset_discovery"]["total_elapsed_ms"] == 20
    assert snapshot["tools"]["asset_discovery"]["cache_hits"] == 1
    assert snapshot["tools"]["asset_discovery"]["cache_misses"] == 1

    metrics.reset()
    assert metrics.snapshot()["model_calls"] == 0
    assert metrics.snapshot()["tool_calls"] == 0


def test_metadata_cache_returns_json_safe_values_and_expires_entries():
    cache = MetadataCache(max_entries=2, ttl_seconds=0.01)
    cache.set("assets:base:usdc", [{"chain": "BASE", "symbol": "USDC"}])

    assert cache.get("assets:base:usdc") == [{"chain": "BASE", "symbol": "USDC"}]
    assert cache.stats()["hits"] == 1

    import time

    time.sleep(0.02)
    assert cache.get("assets:base:usdc") is None
    assert cache.stats()["misses"] == 1


def test_metadata_cache_evicts_oldest_entry_at_capacity():
    cache = MetadataCache(max_entries=1, ttl_seconds=60)
    cache.set("first", {"symbol": "ETH"})
    cache.set("second", {"symbol": "USDC"})

    assert cache.get("first") is None
    assert cache.get("second") == {"symbol": "USDC"}
    assert cache.stats()["evictions"] == 1
