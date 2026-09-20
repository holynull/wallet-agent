import json
from dataclasses import replace

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
    session_evidence = dict(session or {})
    if session is not None:
        session_evidence.setdefault("session_id", "s")
    results = _lifecycle_invariants(
        events=events,
        steps=steps,
        construction_manifest=manifest
        if manifest is not None
        else {
            "wallet_injected_into_graph": False,
            "wallet_injected_into_app": False,
        },
        session=session_evidence,
        max_attempts=3,
    )
    return next(result for result in results if result.name == name)


def _selection_request(sequence, request_id, reference):
    return {
        "sequence": sequence,
        "actor": "public_http",
        "operation": "request",
        "request_id": request_id,
        "method": "POST",
        "path": "/v1/swap/s/select-quote",
        "body": {"provider_reference": reference},
    }


def _request_result(sequence, request_id, status):
    return {
        "sequence": sequence,
        "actor": "public_http",
        "operation": "request_result",
        "request_id": request_id,
        "method": "POST",
        "path": "/v1/swap/s/select-quote",
        "http_status": status,
    }


def _prepare(sequence, reference):
    return {
        "sequence": sequence,
        "actor": "provider",
        "operation": "prepare",
        "provider_reference": reference,
    }


@pytest.mark.parametrize("scenario_id", SCENARIOS)
@pytest.mark.parametrize("max_attempts", [0, -1])
@pytest.mark.asyncio
async def test_run_scenario_rejects_non_positive_max_attempts(
    scenario_id, max_attempts
):
    with pytest.raises(ValueError, match=r"^max_attempts must be >= 1$"):
        await run_scenario(scenario_id, max_attempts=max_attempts)


@pytest.mark.parametrize("max_attempts", [0, -1])
@pytest.mark.asyncio
async def test_drive_scenario_rejects_non_positive_max_attempts(max_attempts):
    runtime = await build_scenario_runtime(SCENARIOS["erc20_swap_without_approval"])
    try:
        with pytest.raises(ValueError, match=r"^max_attempts must be >= 1$"):
            await drive_scenario(runtime, max_attempts=max_attempts)
    finally:
        await runtime.http.aclose()


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


@pytest.mark.parametrize(
    ("outbound_request", "sentinel"),
    [
        (
            httpx.Request(
                "POST",
                "http://wallet.test/v1/agent/turn",
                headers={"content-type": "application/x-www-form-urlencoded"},
                content="private_key=FORM_PRIVATE_KEY_SENTINEL",
            ),
            "FORM_PRIVATE_KEY_SENTINEL",
        ),
        (
            httpx.Request(
                "POST",
                "http://wallet.test/v1/agent/turn",
                json={
                    "metadata": json.dumps(
                        {"private_key": "JSON_STRING_PRIVATE_KEY_SENTINEL"}
                    )
                },
            ),
            "JSON_STRING_PRIVATE_KEY_SENTINEL",
        ),
        (
            httpx.Request(
                "POST",
                "http://wallet.test/v1/agent/turn",
                headers={"X-API-Key": "HEADER_API_KEY_SENTINEL"},
            ),
            "HEADER_API_KEY_SENTINEL",
        ),
        (
            httpx.Request(
                "POST",
                "http://wallet.test/v1/agent/turn",
                headers={"content-type": "application/octet-stream"},
                content=b"OPAQUE_BODY_SENTINEL",
            ),
            "OPAQUE_BODY_SENTINEL",
        ),
        (
            httpx.Request(
                "POST",
                "http://wallet.test/v1/agent/turn",
                headers={"content-type": "application/json"},
                content=json.dumps("OPAQUE_JSON_STRING_SENTINEL"),
            ),
            "OPAQUE_JSON_STRING_SENTINEL",
        ),
        (
            httpx.Request(
                "POST",
                "http://wallet.test/v1/agent/turn",
                json={
                    "metadata": (
                        '{"private_key":"MALFORMED_JSON_PRIVATE_KEY_SENTINEL"'
                    )
                },
            ),
            "MALFORMED_JSON_PRIVATE_KEY_SENTINEL",
        ),
    ],
)
@pytest.mark.asyncio
async def test_public_request_capture_never_reports_private_or_opaque_material(
    outbound_request, sentinel
):
    ledger = EvidenceLedger()

    await _public_request_recorder(ledger)(outbound_request)

    event = ledger.events[0]
    assert event["private_material_detected"] is True
    result = _invariant("no_signing_material_to_server", events=ledger.events)
    recorded_report = json.dumps(result.evidence)
    assert sentinel not in json.dumps(event)
    assert sentinel not in recorded_report
    assert not result.passed


@pytest.mark.asyncio
async def test_public_request_capture_accepts_ordinary_erc20_token_metadata():
    ledger = EvidenceLedger()
    request = httpx.Request(
        "POST",
        "http://wallet.test/v1/agent/turn",
        json={
            "metadata": {
                "token_metadata": {
                    "address": "0x" + "3" * 40,
                    "symbol": "USDC",
                    "decimals": 6,
                }
            }
        },
    )

    await _public_request_recorder(ledger)(request)

    assert ledger.events[0]["private_material_detected"] is False
    assert _invariant(
        "no_signing_material_to_server", events=ledger.events
    ).passed


@pytest.mark.parametrize(
    "header_name",
    [
        "X-Access-Token",
        "X-Api-Key",
        "X-Client-Secret",
        "X-Auth",
        "X-Auth-Secret",
        "X-Custom-Auth-Token",
    ],
)
@pytest.mark.asyncio
async def test_public_request_capture_redacts_credential_header_aliases(header_name):
    sentinel = "CREDENTIAL_HEADER_SENTINEL"
    ledger = EvidenceLedger()
    request = httpx.Request(
        "POST",
        "http://wallet.test/v1/agent/turn",
        headers={header_name: sentinel},
    )

    await _public_request_recorder(ledger)(request)

    event = ledger.events[0]
    report = _invariant("no_signing_material_to_server", events=ledger.events)
    assert event["private_material_detected"] is True
    assert sentinel not in json.dumps(event)
    assert sentinel not in json.dumps(report.evidence)
    assert not report.passed


@pytest.mark.asyncio
async def test_public_request_capture_summarizes_unknown_headers_without_flagging_them():
    sentinel = "UNKNOWN_HEADER_SENTINEL"
    ledger = EvidenceLedger()
    request = httpx.Request(
        "POST",
        "http://wallet.test/v1/agent/turn",
        headers={"X-Request-Context": sentinel},
    )

    await _public_request_recorder(ledger)(request)

    event = ledger.events[0]
    assert event["private_material_detected"] is False
    assert event["headers"]["x-request-context"] == {
        "omitted": True,
        "length": len(sentinel),
    }
    assert sentinel not in json.dumps(event)


@pytest.mark.asyncio
async def test_public_request_capture_decodes_twice_encoded_json_strings():
    sentinel = "TWICE_ENCODED_PRIVATE_KEY_SENTINEL"
    nested = json.dumps(json.dumps({"private_key": sentinel}))
    ledger = EvidenceLedger()
    request = httpx.Request(
        "POST",
        "http://wallet.test/v1/agent/turn",
        json={"metadata": nested},
    )

    await _public_request_recorder(ledger)(request)

    event = ledger.events[0]
    report = _invariant("no_signing_material_to_server", events=ledger.events)
    assert event["private_material_detected"] is True
    assert event["body"]["metadata"] == {"private_key": "[REDACTED]"}
    assert sentinel not in json.dumps(event)
    assert sentinel not in json.dumps(report.evidence)
    assert not report.passed


@pytest.mark.asyncio
async def test_public_request_capture_bounds_nested_json_string_decoding():
    sentinel = "DEPTH_LIMIT_SENTINEL"
    nested = sentinel
    for _ in range(12):
        nested = json.dumps(nested)
    ledger = EvidenceLedger()
    request = httpx.Request(
        "POST",
        "http://wallet.test/v1/agent/turn",
        json={"metadata": nested},
    )

    await _public_request_recorder(ledger)(request)

    event = ledger.events[0]
    report = _invariant("no_signing_material_to_server", events=ledger.events)
    assert event["private_material_detected"] is True
    assert event["body"]["metadata"]["omitted"] is True
    assert sentinel not in json.dumps(event)
    assert sentinel not in json.dumps(report.evidence)
    assert not report.passed


@pytest.mark.asyncio
async def test_public_request_capture_omits_oversized_json_before_recording_values():
    sentinel = "OVERSIZED_BODY_SENTINEL"
    ledger = EvidenceLedger()
    request = httpx.Request(
        "POST",
        "http://wallet.test/v1/agent/turn",
        json={"metadata": ["public-value"] * 8_000 + [sentinel]},
    )

    await _public_request_recorder(ledger)(request)

    event = ledger.events[0]
    report = _invariant("no_signing_material_to_server", events=ledger.events)
    assert event["private_material_detected"] is True
    assert event["body"]["omitted"] is True
    assert sentinel not in json.dumps(event)
    assert sentinel not in json.dumps(report.evidence)
    assert not report.passed


@pytest.mark.parametrize(
    "method",
    [
        "eth_chainId",
        "eth_accounts",
        "wallet_switchEthereumChain",
        "wallet_addEthereumChain",
        "eth_sendTransaction",
        "eth_sendRawTransaction",
        "personal_sign",
        "eth_sign",
        "eth_signTransaction",
        "eth_signTypedData",
        "eth_signTypedData_v1",
        "eth_signTypedData_v3",
        "eth_signTypedData_v4",
    ],
)
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
    events = (
        _selection_request(1, "selection-1", "quote-selected"),
        _request_result(2, "selection-1", 200),
        _prepare(3, "quote-other"),
    )

    result = _invariant(
        "explicit_quote_selection",
        events=events,
        session={"quote_candidates": [{}, {}]},
    )

    assert not result.passed


def test_quote_selection_invariant_uses_latest_successful_selection():
    events = (
        _selection_request(1, "selection-a", "quote-a"),
        _request_result(2, "selection-a", 200),
        _selection_request(3, "selection-b", "quote-b"),
        _request_result(4, "selection-b", 200),
        _prepare(5, "quote-a"),
    )

    result = _invariant(
        "explicit_quote_selection",
        events=events,
        session={"quote_candidates": [{}, {}]},
    )

    assert not result.passed


def test_quote_selection_invariant_accepts_latest_mixed_successful_selection():
    events = (
        _selection_request(3, "selection-b", "quote-b"),
        _request_result(4, "selection-b", 204),
        _selection_request(1, "selection-a", "quote-a"),
        _request_result(2, "selection-a", 200),
        _prepare(5, "quote-b"),
    )

    assert _invariant(
        "explicit_quote_selection",
        events=events,
        session={"quote_candidates": [{}, {}]},
    ).passed


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/swap/s/select-quote"),
        ("POST", "/unexpected/v1/swap/s/select-quote"),
    ],
)
def test_quote_selection_invariant_requires_post_to_exact_session_endpoint(method, path):
    events = (
        {
            **_selection_request(1, "selection-endpoint", "quote-a"),
            "method": method,
            "path": path,
        },
        {
            **_request_result(2, "selection-endpoint", 200),
            "method": method,
            "path": path,
        },
        _prepare(3, "quote-a"),
    )

    assert not _invariant(
        "explicit_quote_selection",
        events=events,
        session={"quote_candidates": [{}, {}]},
    ).passed


@pytest.mark.parametrize(
    ("field", "action", "value"),
    [
        ("method", "omit", None),
        ("path", "omit", None),
        ("method", "replace", None),
        ("path", "replace", None),
        ("method", "replace", ""),
        ("path", "replace", ""),
    ],
    ids=[
        "method-omitted",
        "path-omitted",
        "method-null",
        "path-null",
        "method-empty",
        "path-empty",
    ],
)
@pytest.mark.asyncio
async def test_incomplete_selection_response_evidence_fails_invariant_and_report(
    field, action, value
):
    runtime = await build_scenario_runtime(SCENARIOS["erc20_swap_without_approval"])
    original_record = runtime.ledger.record

    def record_with_incomplete_selection_result(actor, operation, **evidence):
        if operation == "request_result" and str(evidence.get("path", "")).endswith(
            "/select-quote"
        ):
            if action == "omit":
                evidence.pop(field)
            else:
                evidence[field] = value
        return original_record(actor, operation, **evidence)

    runtime.ledger.record = record_with_incomplete_selection_result
    try:
        report = await drive_scenario(runtime)
    finally:
        await runtime.http.aclose()

    selection_invariant = next(
        result for result in report.invariants if result.name == "explicit_quote_selection"
    )
    assert report.failures == ()
    assert not selection_invariant.passed
    assert report.status == "failed"


@pytest.mark.parametrize(
    "events",
    [
        (
            {
                "sequence": 1,
                "actor": "public_http",
                "operation": "request",
                "request_id": "shared-id",
                "method": "GET",
                "path": "/v1/session/s",
            },
            _selection_request(2, "shared-id", "quote-a"),
            _request_result(3, "shared-id", 200),
            _prepare(4, "quote-a"),
        ),
        (
            _request_result(1, "selection-before", 200),
            _selection_request(2, "selection-before", "quote-a"),
            _prepare(3, "quote-a"),
        ),
        (
            _selection_request(1, "selection-equal", "quote-a"),
            _request_result(1, "selection-equal", 200),
            _prepare(2, "quote-a"),
        ),
        (
            _selection_request(1, "selection-wrong-path", "quote-a"),
            {
                **_request_result(2, "selection-wrong-path", 200),
                "path": "/v1/session/s",
            },
            _prepare(3, "quote-a"),
        ),
        (
            _selection_request(1, "selection-wrong-method", "quote-a"),
            {
                **_request_result(2, "selection-wrong-method", 200),
                "method": "GET",
            },
            _prepare(3, "quote-a"),
        ),
        (
            _selection_request(1, "selection-public-sequence", "quote-a"),
            _request_result(2, "selection-public-sequence", 200),
            {
                "sequence": 2,
                "actor": "public_http",
                "operation": "request_result",
                "request_id": "unrelated-result",
                "method": "GET",
                "path": "/v1/session/s",
                "http_status": 200,
            },
            _prepare(3, "quote-a"),
        ),
        (
            {
                **_selection_request("1", "selection-string-sequence", "quote-a"),
            },
            _request_result(2, "selection-string-sequence", 200),
            _prepare(3, "quote-a"),
        ),
    ],
)
def test_quote_selection_invariant_rejects_ambiguous_public_request_correlation(events):
    assert not _invariant(
        "explicit_quote_selection",
        events=events,
        session={"quote_candidates": [{}, {}]},
    ).passed


@pytest.mark.parametrize(
    "events",
    [
        (
            _selection_request(1, "selection-failed", "quote-a"),
            _request_result(2, "selection-failed", 409),
            _prepare(3, "quote-a"),
            _selection_request(4, "selection-later", "quote-a"),
            _request_result(5, "selection-later", 200),
        ),
        (
            _selection_request(1, "selection-unpaired", "quote-a"),
            _prepare(2, "quote-a"),
        ),
        (
            _selection_request(1, "selection-duplicate", "quote-a"),
            _selection_request(2, "selection-duplicate", "quote-a"),
            _request_result(3, "selection-duplicate", 200),
            _prepare(4, "quote-a"),
        ),
        (
            _selection_request(1, "selection-two-results", "quote-a"),
            _request_result(2, "selection-two-results", 200),
            _request_result(3, "selection-two-results", None),
            _prepare(4, "quote-a"),
        ),
        (
            _selection_request(1, "selection-sequence-a", "quote-a"),
            _request_result(2, "selection-sequence-a", 200),
            _selection_request(1, "selection-sequence-b", "quote-b"),
            _request_result(3, "selection-sequence-b", 200),
            _prepare(4, "quote-b"),
        ),
        (
            _selection_request(1, "selection-late-result", "quote-a"),
            _prepare(2, "quote-a"),
            _request_result(3, "selection-late-result", 200),
        ),
    ],
)
def test_quote_selection_invariant_rejects_failed_unpaired_or_duplicate_requests(events):
    result = _invariant(
        "explicit_quote_selection",
        events=events,
        steps=(
            _step(
                "select_quote",
                {"selected_quote": {"provider_reference": "quote-a"}},
            ),
        ),
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


@pytest.mark.parametrize(
    ("scenario_id", "final_stage"),
    [
        ("approval_pending_restart_resume", "completed"),
        ("wallet_rejects_approval", "approval_required"),
        ("wallet_rejects_swap", "swap_ready"),
        ("swap_temporarily_not_visible", "completed"),
        ("swap_reverted", "failed"),
    ],
)
@pytest.mark.asyncio
async def test_recovery_and_failure_scenarios_are_safe(scenario_id, final_stage):
    report = await run_scenario(scenario_id)

    assert report.status == "passed"
    assert report.final_stage == final_stage
    assert all(result.passed for result in report.invariants)


@pytest.mark.asyncio
async def test_restart_uses_only_public_session_projection():
    report = await run_scenario("approval_pending_restart_resume")
    operations = [step.operation for step in report.steps]

    assert "client_restart" in operations
    restart = next(step for step in report.steps if step.operation == "client_restart")
    assert set(restart.evidence) == {"conversation_id", "session_id"}
    assert operations.index("client_restart") < operations.index("continue")


@pytest.mark.asyncio
async def test_restart_waits_for_approval_receipt_before_swap_preparation():
    runtime = await build_scenario_runtime(SCENARIOS["approval_pending_restart_resume"])
    try:
        report = await drive_scenario(runtime)
    finally:
        await runtime.http.aclose()

    approval_hash = "0x" + "d" * 64
    continue_steps = [step for step in report.steps if step.operation == "continue"]
    assert [step.stage_after for step in continue_steps] == [
        "approval_pending",
        "approval_pending",
        "swap_ready",
    ]
    restart_index = next(
        index
        for index, step in enumerate(report.steps)
        if step.operation == "client_restart"
    )
    continue_indices = [
        index for index, step in enumerate(report.steps) if step.operation == "continue"
    ]
    assert len(continue_indices) <= 3
    assert all(restart_index < index for index in continue_indices)

    prepare_sequence = next(
        event["sequence"]
        for event in runtime.ledger.events
        if event.get("actor") == "provider" and event.get("operation") == "prepare"
    )
    approval_receipts = [
        event
        for event in runtime.ledger.events
        if event.get("actor") == "chain"
        and event.get("operation") == "get_transaction_receipt"
        and event.get("sequence", prepare_sequence) < prepare_sequence
    ]
    assert [event["tx_hash"] for event in approval_receipts] == [approval_hash] * 3
    assert [event["outcome"]["kind"] for event in approval_receipts] == [
        "not_found",
        "pending",
        "confirmed",
    ]


@pytest.mark.parametrize("scenario_id", ["wallet_rejects_approval", "wallet_rejects_swap"])
@pytest.mark.asyncio
async def test_wallet_rejection_never_registers_provider_order(scenario_id):
    report = await run_scenario(scenario_id)

    assert [
        call
        for call in report.provider_calls
        if call["operation"] == "register_broadcast"
    ] == []
    assert "wallet_rejected" in [step.error_code for step in report.steps]


@pytest.mark.asyncio
async def test_real_approval_rejection_is_the_only_valid_no_prepare_exemption():
    report = await run_scenario("wallet_rejects_approval")

    selection = next(
        result
        for result in report.invariants
        if result.name == "explicit_quote_selection"
    )
    assert selection.passed
    assert report.status == "passed"


@pytest.mark.parametrize("operation", ["wallet_swap", "unrelated"])
def test_no_prepare_exemption_rejects_unrelated_code_4001(operation):
    approval_transaction = {
        "from": "0x" + "1" * 40,
        "to": "0x" + "3" * 40,
        "data": "0x095ea7b3",
        "value": "0x0",
    }
    events = (
        _selection_request(1, "selection-approval", "quote-a"),
        _request_result(2, "selection-approval", 200),
        {
            "sequence": 3,
            "actor": "wallet",
            "operation": "eth_sendTransaction",
            "method": "eth_sendTransaction",
            "params": [approval_transaction],
        },
    )
    steps = (
        LifecycleStep(
            "select_quote",
            "selecting_quote",
            "confirmation_required",
            200,
            None,
            {},
        ),
        LifecycleStep(
            "confirm",
            "confirmation_required",
            "approval_required",
            200,
            None,
            {},
        ),
        LifecycleStep(
            operation,
            "approval_required",
            "approval_required",
            None,
            "wallet_rejected",
            {
                "code": 4001,
                "method": "eth_sendTransaction",
                "transaction": approval_transaction,
            },
        ),
    )

    result = _invariant(
        "explicit_quote_selection",
        events=events,
        steps=steps,
        session={"quote_candidates": [{}, {}]},
    )

    assert not result.passed


@pytest.mark.asyncio
async def test_swap_rejection_without_prepare_fails_invariant_and_report():
    runtime = await build_scenario_runtime(SCENARIOS["wallet_rejects_swap"])
    original_record = runtime.ledger.record

    def record_without_prepare(actor, operation, **evidence):
        if actor == "provider" and operation == "prepare":
            return {"actor": actor, "operation": operation, **evidence}
        return original_record(actor, operation, **evidence)

    runtime.ledger.record = record_without_prepare
    try:
        report = await drive_scenario(runtime)
    finally:
        await runtime.http.aclose()

    selection = next(
        result
        for result in report.invariants
        if result.name == "explicit_quote_selection"
    )
    assert report.failures == ()
    assert not selection.passed
    assert report.status == "failed"


@pytest.mark.asyncio
async def test_pending_recovery_rejects_completed_session_with_changed_hash(monkeypatch):
    runtime = await build_scenario_runtime(SCENARIOS["swap_temporarily_not_visible"])
    original_session = runtime.client.session
    changed_hash = "0x" + "8" * 64

    async def session_with_changed_completed_hash():
        result = await original_session()
        if result.get("stage") == "completed":
            result = {**result, "broadcast_tx_hash": changed_hash}
            runtime.client.steps[-1] = replace(
                runtime.client.steps[-1], evidence=result
            )
        return result

    monkeypatch.setattr(runtime.client, "session", session_with_changed_completed_hash)
    try:
        report = await drive_scenario(runtime)
    finally:
        await runtime.http.aclose()

    pending = next(
        result
        for result in report.invariants
        if result.name == "pending_is_recoverable"
    )
    assert report.failures == ()
    assert not pending.passed
    assert report.status == "failed"


@pytest.mark.asyncio
async def test_reverted_swap_is_not_registered_or_reported_successful():
    report = await run_scenario("swap_reverted")

    broadcast = next(step for step in report.steps if step.operation == "broadcast")
    assert broadcast.http_status == 409
    assert broadcast.error_code == "TRANSACTION_FAILED"
    assert [
        call
        for call in report.provider_calls
        if call["operation"] == "register_broadcast"
    ] == []


@pytest.mark.parametrize(
    "scenario_id",
    [
        "duplicate_and_conflicting_swap_hash",
        "provider_register_timeout_then_retry",
        "omnibridge_erc20_deposit_order",
    ],
)
@pytest.mark.asyncio
async def test_retry_and_omni_scenarios_pass_all_invariants(scenario_id):
    report = await run_scenario(scenario_id)

    assert report.status == "passed"
    assert report.failures == ()
    assert all(result.passed for result in report.invariants)


@pytest.mark.asyncio
async def test_duplicate_hash_is_idempotent_and_conflicting_hash_is_rejected():
    report = await run_scenario("duplicate_and_conflicting_swap_hash")
    broadcasts = [step for step in report.steps if step.operation == "broadcast"]
    registrations = [
        call
        for call in report.provider_calls
        if call["operation"] == "register_broadcast"
    ]

    assert [step.http_status for step in broadcasts] == [200, 200, 409]
    assert broadcasts[2].error_code == "HTTP_409"
    assert len(registrations) == 1
    assert registrations[0]["tx_hash"] == "0x" + "d" * 64


@pytest.mark.asyncio
async def test_provider_timeout_retry_commits_one_order_for_same_wallet_hash():
    report = await run_scenario("provider_register_timeout_then_retry")
    attempts = [
        call
        for call in report.provider_calls
        if call["operation"] == "register_broadcast"
    ]

    assert [call["outcome"] for call in attempts] == ["timeout", "success"]
    assert len([call for call in attempts if call.get("provider_order_id")]) == 1
    assert (
        len(
            [
                call
                for call in report.wallet_calls
                if call["method"] == "eth_sendTransaction"
            ]
        )
        == 1
    )
    timeout = next(
        step
        for step in report.steps
        if step.error_code == "PROVIDER_REGISTRATION_FAILED"
    )
    assert timeout.http_status == 503
    assert timeout.evidence["retryable"] is True


@pytest.mark.asyncio
async def test_omni_erc20_deposit_uses_order_reference_and_token_transfer():
    report = await run_scenario("omnibridge_erc20_deposit_order")
    wallet_send = next(
        call
        for call in report.wallet_calls
        if call["method"] == "eth_sendTransaction"
    )
    registration = next(
        call
        for call in report.provider_calls
        if call["operation"] == "register_broadcast"
    )

    assert wallet_send["params"][0]["to"] == "0x" + "3" * 40
    assert wallet_send["params"][0]["data"]["selector"] == "0xa9059cbb"
    assert registration["provider_reference"] == "omni-deposit-order-1"
    assert registration["provider_reference"] != "omni-quote-1"
