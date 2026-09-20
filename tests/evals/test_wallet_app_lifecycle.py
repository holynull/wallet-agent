import pytest

from evals.wallet_app_scenarios import run_scenario


@pytest.mark.parametrize("scenario_id", ["erc20_swap_without_approval", "erc20_swap_with_approval"])
@pytest.mark.asyncio
async def test_happy_paths_complete_through_public_wallet_app_contract(scenario_id):
    report = await run_scenario(scenario_id)

    assert report.status == "passed"
    assert report.final_stage == "completed"
    assert report.failures == ()
    assert all(result.passed for result in report.invariants)


@pytest.mark.asyncio
async def test_approval_happy_path_orders_wallet_and_provider_side_effects():
    report = await run_scenario("erc20_swap_with_approval")

    send_calls = [call for call in report.wallet_calls if call["method"] == "eth_sendTransaction"]
    register_calls = [
        call for call in report.provider_calls if call["operation"] == "register_broadcast"
    ]
    assert len(send_calls) == 2
    assert send_calls[0]["params"][0]["to"] == "0x" + "3" * 40
    assert send_calls[1]["params"][0]["to"] == "0x" + "5" * 40
    assert len(register_calls) == 1
    assert register_calls[0]["tx_hash"] == "0x" + "b" * 64
    assert [step.operation for step in report.steps] == [
        "turn",
        "stream",
        "session",
        "select_quote",
        "confirm",
        "wallet_approval",
        "approve_broadcast",
        "continue",
        "wallet_swap",
        "broadcast",
        "status_turn",
        "stream",
        "session",
    ]
