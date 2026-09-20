import httpx
import pytest

from evals.wallet_app_scenarios import (
    SCENARIOS,
    _lifecycle_invariants,
    _public_request_recorder,
    _safe_evidence,
    build_scenario_runtime,
    drive_scenario,
    run_scenario,
)
from evals.wallet_app_simulator import EvidenceLedger, LifecycleStep


def _step(operation, evidence=None, *, status=200):
    return LifecycleStep(operation, None, None, status, None, evidence or {})


def _invariant(name, *, events=(), steps=(), manifest=None, session=None):
    results = _lifecycle_invariants(
        events=events,
        steps=steps,
        construction_manifest=manifest
        if manifest is not None
        else {
            "wallet_injected_into_graph": False,
            "wallet_injected_into_app": False,
        },
        session=session or {},
        max_attempts=3,
    )
    return next(result for result in results if result.name == name)


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


@pytest.mark.asyncio
async def test_failed_invariant_fails_scenario_even_without_execution_failure(monkeypatch):
    monkeypatch.setattr("evals.wallet_app_scenarios._safe_evidence", lambda _value: False)

    report = await run_scenario("erc20_swap_without_approval")

    assert report.failures == ()
    assert report.status == "failed"


@pytest.mark.parametrize(
    "evidence",
    [
        {"request": {"metadata": {"private_key": "secret"}}},
        {"request": {"metadata": {"kind": "seed_phrase"}}},
        {"request": {"metadata": {"client_secret": "[REDACTED]"}}},
    ],
)
def test_signing_material_invariant_recurses_through_public_evidence(evidence):
    result = _invariant(
        "no_signing_material_to_server",
        events=({"actor": "public_http", "operation": "request", **evidence},),
    )

    assert result.passed is ("[REDACTED]" in str(evidence))


def test_safe_evidence_accepts_only_redacted_forbidden_fields():
    assert _safe_evidence({"outer": [{"signature": "[REDACTED]"}]})
    assert not _safe_evidence({"outer": [{"label": "signed_raw_transaction"}]})


@pytest.mark.asyncio
async def test_public_request_capture_marks_and_redacts_private_material():
    ledger = EvidenceLedger()
    request = httpx.Request(
        "POST",
        "http://wallet.test/v1/agent/turn",
        json={"metadata": {"private_key": "do-not-report"}},
    )

    await _public_request_recorder(ledger)(request)

    event = ledger.events[0]
    assert event["private_material_detected"] is True
    assert event["body"] == {"metadata": {"private_key": "[REDACTED]"}}
    assert "do-not-report" not in str(event)


@pytest.mark.parametrize("method", ["personal_sign", "wallet_switchEthereumChain"])
def test_server_wallet_action_invariant_recognizes_non_eth_wallet_methods(method):
    result = _invariant(
        "no_server_wallet_actions",
        events=({"sequence": 1, "actor": "server", "operation": method},),
    )

    assert not result.passed


def test_server_wallet_action_invariant_recurses_into_request_evidence():
    result = _invariant(
        "no_server_wallet_actions",
        events=(
            {
                "sequence": 1,
                "actor": "server",
                "operation": "request",
                "arguments": {"request": {"method": "eth_signTypedData_v4"}},
            },
        ),
    )

    assert not result.passed


def test_server_wallet_action_invariant_requires_explicit_false_manifest_entries():
    result = _invariant("no_server_wallet_actions", manifest={})

    assert not result.passed


@pytest.mark.parametrize(
    "events",
    [
        (
            {
                "sequence": 1,
                "actor": "wallet",
                "operation": "eth_sendTransaction_result",
                "result": "0xapproval",
                "success": True,
            },
            {
                "sequence": 2,
                "actor": "public_http",
                "operation": "request",
                "path": "/v1/swap/s/approve-broadcast",
                "body": {"approve_tx_hash": "0xapproval"},
            },
            {
                "sequence": 3,
                "actor": "provider",
                "operation": "register_broadcast",
                "tx_hash": "0xswap",
            },
        ),
        (
            {
                "sequence": 1,
                "actor": "wallet",
                "operation": "eth_sendTransaction_result",
                "result": "0xother",
                "success": True,
            },
            {
                "sequence": 2,
                "actor": "public_http",
                "operation": "request",
                "path": "/v1/swap/s/broadcast",
                "body": {"tx_hash": "0xswap"},
            },
            {
                "sequence": 3,
                "actor": "provider",
                "operation": "register_broadcast",
                "tx_hash": "0xswap",
            },
        ),
        (
            {
                "sequence": 1,
                "actor": "wallet",
                "operation": "eth_sendTransaction_result",
                "result": "0xswap",
                "success": False,
            },
            {
                "sequence": 2,
                "actor": "public_http",
                "operation": "request",
                "path": "/v1/swap/s/broadcast",
                "body": {"tx_hash": "0xswap"},
            },
            {
                "sequence": 3,
                "actor": "provider",
                "operation": "register_broadcast",
                "tx_hash": "0xswap",
            },
        ),
        (
            {
                "sequence": 1,
                "actor": "wallet",
                "operation": "eth_sendTransaction_result",
                "result": "0xswap",
                "success": True,
            },
            {
                "sequence": 2,
                "actor": "public_http",
                "operation": "request",
                "path": "/v1/swap/s/broadcast",
                "body": {"tx_hash": "0xswap"},
            },
            {
                "sequence": 3,
                "actor": "provider",
                "operation": "register_broadcast",
                "tx_hash": "0xswap",
            },
            {
                "sequence": 4,
                "actor": "provider",
                "operation": "register_broadcast",
                "tx_hash": "0xextra",
            },
        ),
    ],
)
def test_register_invariant_rejects_unmatched_or_extra_registration(events):
    result = _invariant("no_register_before_wallet_hash", events=events)

    assert not result.passed


def test_register_invariant_matches_each_registration_to_result_and_public_submission():
    events = (
        {
            "sequence": 1,
            "actor": "wallet",
            "operation": "eth_sendTransaction_result",
            "result": "0xswap",
            "success": True,
        },
        {
            "sequence": 2,
            "actor": "public_http",
            "operation": "request",
            "path": "/v1/swap/s/broadcast",
            "body": {"tx_hash": "0xswap"},
        },
        {
            "sequence": 3,
            "actor": "provider",
            "operation": "register_broadcast",
            "tx_hash": "0xswap",
        },
    )

    assert _invariant("no_register_before_wallet_hash", events=events).passed


def test_quote_selection_invariant_requires_selected_reference_before_prepare():
    steps = (
        _step(
            "select_quote",
            {"selected_quote": {"provider_reference": "quote-selected"}},
        ),
    )
    events = (
        {
            "sequence": 1,
            "actor": "public_http",
            "operation": "request",
            "path": "/v1/swap/s/select-quote",
            "body": {"provider_reference": "quote-selected"},
        },
        {
            "sequence": 2,
            "actor": "provider",
            "operation": "prepare",
            "provider_reference": "quote-other",
        },
    )

    result = _invariant(
        "explicit_quote_selection",
        events=events,
        steps=steps,
        session={"quote_candidates": [{}, {}]},
    )

    assert not result.passed


def test_quote_selection_invariant_uses_latest_successful_selection():
    steps = (
        _step("select_quote", {"selected_quote": {"provider_reference": "quote-a"}}),
        _step("select_quote", {"selected_quote": {"provider_reference": "quote-b"}}),
    )
    events = (
        {
            "sequence": 1,
            "actor": "public_http",
            "operation": "select_quote_result",
            "provider_reference": "quote-a",
            "success": True,
        },
        {
            "sequence": 2,
            "actor": "public_http",
            "operation": "select_quote_result",
            "provider_reference": "quote-b",
            "success": True,
        },
        {
            "sequence": 3,
            "actor": "provider",
            "operation": "prepare",
            "provider_reference": "quote-a",
        },
    )

    result = _invariant(
        "explicit_quote_selection",
        events=events,
        steps=steps,
        session={"quote_candidates": [{}, {}]},
    )

    assert not result.passed


@pytest.mark.asyncio
async def test_status_turn_label_uses_turn_start_index(monkeypatch):
    runtime = await build_scenario_runtime(SCENARIOS["erc20_swap_without_approval"])
    original_turn = runtime.client.turn
    calls = 0

    async def turn_with_intervening_step(message):
        nonlocal calls
        calls += 1
        result = await original_turn(message)
        if calls == 2:
            runtime.client.steps.insert(-1, _step("intervening"))
        return result

    monkeypatch.setattr(runtime.client, "turn", turn_with_intervening_step)
    try:
        report = await drive_scenario(runtime)
    finally:
        await runtime.http.aclose()

    assert [step.operation for step in report.steps].count("status_turn") == 1
    assert "intervening" in [step.operation for step in report.steps]
