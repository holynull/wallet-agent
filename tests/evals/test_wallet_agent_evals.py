import pytest

from evals.wallet_agent_evals import evaluate_expectations, run_offline_evals


@pytest.mark.asyncio
async def test_offline_evals_cover_balance_transfer_and_swap_capabilities():
    report = await run_offline_evals()

    assert report["summary"] == {"total": 15, "passed": 15, "pass_rate": 1.0}
    assert report["capabilities"] == {
        "balance": {"total": 2, "passed": 2, "pass_rate": 1.0},
        "transfer": {"total": 3, "passed": 3, "pass_rate": 1.0},
        "swap": {"total": 10, "passed": 10, "pass_rate": 1.0},
    }
    assert report["dimensions"] == {
        "asset_and_chain_resolution": {"total": 1, "passed": 1, "failed": 0, "pass_rate": 1.0},
        "conversation_understanding": {"total": 1, "passed": 1, "failed": 0, "pass_rate": 1.0},
        "state_and_resume": {"total": 1, "passed": 1, "failed": 0, "pass_rate": 1.0},
    }
    assert {item["id"] for item in report["cases"]} == {
        "balance_native",
        "balance_portfolio",
        "transfer_native_prepare",
        "transfer_missing_recipient",
        "swap_quote",
        "swap_multiturn_slots",
        "swap_missing_amount",
        "swap_target_direction_memory",
        "swap_followup_clarification_keeps_slots",
        "swap_chain_correction_invalidates_quote",
        "swap_cancel_clears_artifacts",
        "balance_during_swap_preserves_task",
        "transfer_compact_amount",
        "swap_symbol_typo",
        "swap_eth_usdt_to_bsc_bnb_multiturn",
    }
    assert all(item["failures"] == [] for item in report["cases"])


@pytest.mark.asyncio
async def test_offline_evals_score_native_asset_multiturn_regression():
    report = await run_offline_evals()
    case = next(
        item
        for item in report["cases"]
        if item["id"] == "swap_eth_usdt_to_bsc_bnb_multiturn"
    )

    assert case["passed"] is True
    assert case["response_kind"] == "swap_quote"
    assert "asset_and_chain_resolution" in case["dimensions"]
    assert report["dimensions"]["asset_and_chain_resolution"]["failed"] == 0


def test_expectation_scoring_reports_wrong_state_and_unsafe_side_effects():
    failures = evaluate_expectations(
        state={"response": {"kind": "error"}},
        expected={"response_kind": "transfer_prepare", "forbid_broadcast": True},
        side_effects={"broadcast_calls": 1},
    )

    assert failures == [
        "response.kind: expected 'transfer_prepare', got 'error'",
        "broadcast_calls: expected 0, got 1",
    ]
