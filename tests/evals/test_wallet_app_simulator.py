import json

import httpx
import pytest

from evals.wallet_app_simulator import (
    Eip1193Error,
    Eip1193WalletSimulator,
    EvaluationHttpError,
    FaultOutcome,
    FaultSequence,
    LifecycleStep,
    WalletAppClient,
    parse_sse,
    sanitize_evidence,
)


async def chunks(*values: bytes):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_sse_parser_handles_split_and_multiple_events_and_final_buffer():
    events = [
        event
        async for event in parse_sse(
            chunks(
                b"event: update\ndata: {\"n\":",
                b"1}\n\nevent: action_required\ndata: {\"stage\":\"confirm\"}\n\n",
                b"data: {\"tail\":true}",
            )
        )
    ]

    assert [(event.name, event.data) for event in events] == [
        ("update", {"n": 1}),
        ("action_required", {"stage": "confirm"}),
        ("message", {"tail": True}),
    ]


@pytest.mark.asyncio
async def test_sse_parser_handles_utf8_code_point_split_across_chunks():
    payload = 'data: {"label":"钱包"}'.encode()
    split_at = payload.index("钱".encode()) + 1

    events = [
        event
        async for event in parse_sse(
            chunks(payload[:split_at], payload[split_at:])
        )
    ]

    assert [(event.name, event.data) for event in events] == [
        ("message", {"label": "钱包"})
    ]


def test_fault_sequence_consumes_order_then_uses_explicit_fallback():
    sequence = FaultSequence(
        outcomes=(FaultOutcome("not_found"), FaultOutcome("confirmed", {"status": "0x1"})),
        fallback=FaultOutcome("rpc_error", message="exhausted"),
    )

    assert [sequence.next().kind for _ in range(3)] == [
        "not_found",
        "confirmed",
        "rpc_error",
    ]


def test_evidence_redaction_is_recursive_without_hiding_erc20_token_metadata():
    evidence = sanitize_evidence(
        {
            "authorization": "Bearer secret",
            "metadata": {"private_key": "0xdead", "apiKey": "provider-secret"},
            "token": {
                "symbol": "USDC",
                "address": "0x" + "3" * 40,
                "raw": {"source": "asset-registry"},
            },
            "data": "0x1234567890abcdef",
        }
    )

    assert evidence == {
        "authorization": "[REDACTED]",
        "metadata": {"private_key": "[REDACTED]", "apiKey": "[REDACTED]"},
        "token": {
            "symbol": "USDC",
            "address": "0x" + "3" * 40,
            "raw": {"source": "asset-registry"},
        },
        "data": {"selector": "0x12345678", "length": 18},
    }
    assert "provider-secret" not in json.dumps(evidence)


@pytest.mark.asyncio
async def test_wallet_switches_chain_and_returns_configured_unsigned_transaction_hash():
    tx_hash = "0x" + "a" * 64
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=("0x" + "1" * 40,),
        send_outcomes=FaultSequence(
            outcomes=(FaultOutcome("success", tx_hash),),
            fallback=FaultOutcome("rpc_error", message="unexpected second send"),
        ),
    )

    await wallet.request("wallet_switchEthereumChain", [{"chainId": "0x38"}])
    result = await wallet.request(
        "eth_sendTransaction",
        [{"from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "data": "0x", "value": "0x0"}],
    )

    assert result == tx_hash
    assert wallet.chain_id == "0x38"
    assert [call["method"] for call in wallet.calls] == [
        "wallet_switchEthereumChain",
        "eth_sendTransaction",
    ]


@pytest.mark.asyncio
async def test_wallet_switch_exhaustion_fails_without_changing_chain():
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=("0x" + "1" * 40,),
        switch_outcomes=FaultSequence(),
    )

    with pytest.raises(Eip1193Error, match="fault sequence exhausted") as exhausted:
        await wallet.request("wallet_switchEthereumChain", [{"chainId": "0x38"}])

    assert exhausted.value.code == -32603
    assert wallet.chain_id == "0x1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        FaultOutcome("reject", code=4001, message="User denied chain switch"),
        FaultOutcome("rpc_error", code=-32000, message="Switch RPC failed"),
    ],
)
async def test_wallet_switch_preserves_configured_failure_without_changing_chain(
    outcome,
):
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=("0x" + "1" * 40,),
        switch_outcomes=FaultSequence(outcomes=(outcome,)),
    )

    with pytest.raises(Eip1193Error, match=outcome.message) as failed:
        await wallet.request("wallet_switchEthereumChain", [{"chainId": "0x38"}])

    assert failed.value.code == outcome.code
    assert wallet.chain_id == "0x1"


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_chain_id", ["56", "0x038"])
async def test_wallet_canonicalizes_chain_id_after_successful_switch(
    requested_chain_id,
):
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=("0x" + "1" * 40,),
    )

    await wallet.request(
        "wallet_switchEthereumChain", [{"chainId": requested_chain_id}]
    )

    assert wallet.chain_id == "0x38"


@pytest.mark.asyncio
async def test_wallet_rejects_signing_material_and_reports_eip1193_user_rejection():
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=("0x" + "1" * 40,),
        send_outcomes=FaultSequence(
            outcomes=(FaultOutcome("reject", code=4001, message="User rejected request"),),
            fallback=FaultOutcome("reject", code=4001, message="User rejected request"),
        ),
    )

    with pytest.raises(ValueError, match="signed transaction material"):
        await wallet.request(
            "eth_sendTransaction",
            [
                {
                    "from": "0x" + "1" * 40,
                    "to": "0x" + "2" * 40,
                    "raw": "0xsigned-secret",
                }
            ],
        )
    assert wallet.calls[0]["params"][0]["raw"] == "[REDACTED]"
    assert "0xsigned-secret" not in json.dumps(wallet.ledger.events)
    with pytest.raises(Eip1193Error) as rejected:
        await wallet.request(
            "eth_sendTransaction",
            [{"from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "data": "0x"}],
        )

    assert rejected.value.code == 4001
    assert sanitize_evidence(wallet.calls)[-1]["params"][0]["to"] == "0x" + "2" * 40


@pytest.mark.asyncio
async def test_wallet_models_switch_failure_and_account_or_chain_change_before_send():
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=("0x" + "1" * 40,),
        switch_outcomes=FaultSequence(
            outcomes=(FaultOutcome("switch_error", code=4902, message="unknown chain"),),
            fallback=FaultOutcome("success"),
        ),
        send_outcomes=FaultSequence(
            outcomes=(
                FaultOutcome("account_change", "0x" + "9" * 40),
                FaultOutcome("chain_change", "0x38"),
            ),
            fallback=FaultOutcome("rpc_error", message="unexpected send"),
        ),
    )

    with pytest.raises(Eip1193Error) as switch_error:
        await wallet.request("wallet_switchEthereumChain", [{"chainId": "0x38"}])
    assert switch_error.value.code == 4902

    with pytest.raises(Eip1193Error, match="account changed"):
        await wallet.request(
            "eth_sendTransaction",
            [{"from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "data": "0x"}],
        )
    assert await wallet.request("eth_accounts") == ["0x" + "9" * 40]

    with pytest.raises(Eip1193Error, match="chain changed"):
        await wallet.request(
            "eth_sendTransaction",
            [{"from": "0x" + "9" * 40, "to": "0x" + "2" * 40, "data": "0x"}],
        )
    assert await wallet.request("eth_chainId") == "0x38"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_chain_id", ["56", "0x038"])
async def test_wallet_canonicalizes_configured_chain_change(changed_chain_id):
    wallet = Eip1193WalletSimulator(
        chain_id="0x1",
        accounts=("0x" + "1" * 40,),
        send_outcomes=FaultSequence(
            outcomes=(FaultOutcome("chain_change", changed_chain_id),),
        ),
    )

    with pytest.raises(Eip1193Error, match="chain changed"):
        await wallet.request(
            "eth_sendTransaction",
            [{"from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "data": "0x"}],
        )

    assert wallet.chain_id == "0x38"


@pytest.mark.asyncio
async def test_wallet_app_client_drives_public_routes_and_can_be_reconstructed():
    requests = []

    async def handler(request):
        requests.append(
            (request.method, request.url.path, request.url.params, request.content)
        )
        if request.url.path == "/v1/agent/turn":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "conversation_id": "conversation-1",
                    "session_id": "session-1",
                    "status": "running",
                },
            )
        if request.url.path == "/v1/agent/stream/run-1":
            return httpx.Response(
                200,
                content=(
                    b'event: update\ndata: {"event":"update","data":'
                    b'{"stage":"selecting_quote"}}\n\n'
                ),
                headers={"content-type": "text/event-stream"},
            )
        if request.url.path == "/v1/swap/session-1":
            return httpx.Response(
                200,
                json={"session_id": "session-1", "stage": "selecting_quote"},
            )
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as http:
        client = WalletAppClient(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
        )
        events = await client.turn("swap")
        restored = WalletAppClient.restore(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
            conversation_id=client.conversation_id,
            session_id=client.session_id,
        )
        session = await restored.session()

    assert events[0].name == "update"
    assert (client.conversation_id, client.session_id, client.run_id) == (
        "conversation-1",
        "session-1",
        "run-1",
    )
    assert session["stage"] == "selecting_quote"
    assert [(method, path) for method, path, _, _ in requests] == [
        ("POST", "/v1/agent/turn"),
        ("GET", "/v1/agent/stream/run-1"),
        ("GET", "/v1/swap/session-1"),
    ]
    assert json.loads(requests[0][3]) == {
        "user_id": "alice",
        "conversation_id": None,
        "session_id": None,
        "message": "swap",
        "address": "0x" + "1" * 40,
        "chain": "ETH",
        "metadata": {},
    }
    assert dict(requests[2][2]) == {"user_id": "alice"}
    assert [step.operation for step in client.steps] == ["turn", "stream"]
    assert client.steps[1].stage_after == "selecting_quote"
    assert [step.operation for step in restored.steps] == ["session"]


@pytest.mark.asyncio
async def test_wallet_app_client_preserves_structured_public_http_errors():
    async def handler(_request):
        return httpx.Response(
            409,
            json={
                "code": "TRANSACTION_FAILED",
                "message": "chain receipt reverted",
                "details": {
                    "tx_hash": "0x" + "f" * 64,
                    "authorization": "secret",
                    "nested": {"client_secret": "nested-secret"},
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as http:
        client = WalletAppClient.restore(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
            conversation_id="conversation-1",
            session_id="session-1",
        )
        with pytest.raises(EvaluationHttpError) as raised:
            await client.submit_swap_hash("ETH", "0x" + "f" * 64)

    assert raised.value.operation == "broadcast"
    assert raised.value.code == "TRANSACTION_FAILED"
    assert raised.value.status_code == 409
    assert raised.value.details["authorization"] == "[REDACTED]"
    assert raised.value.details["nested"]["client_secret"] == "[REDACTED]"
    assert client.steps == [
        LifecycleStep(
            operation="broadcast",
            stage_before=None,
            stage_after=None,
            http_status=409,
            error_code="TRANSACTION_FAILED",
            evidence={
                "tx_hash": "0x" + "f" * 64,
                "authorization": "[REDACTED]",
                "nested": {"client_secret": "[REDACTED]"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_wallet_app_client_wraps_non_object_json_http_errors():
    async def handler(_request):
        return httpx.Response(502, json=["upstream failed"])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as http:
        client = WalletAppClient.restore(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
            conversation_id="conversation-1",
            session_id="session-1",
        )
        with pytest.raises(EvaluationHttpError) as raised:
            await client.session()

    assert raised.value.code == "HTTP_502"
    assert raised.value.status_code == 502
    assert raised.value.details == {}
    assert client.steps[0].error_code == "HTTP_502"


@pytest.mark.asyncio
async def test_wallet_app_client_records_rest_redirect_with_following_http_client():
    requested_paths = []

    async def handler(request):
        requested_paths.append(request.url.path)
        if request.url.path == "/v1/swap/session-1":
            return httpx.Response(
                307,
                json={
                    "code": "SESSION_REDIRECTED",
                    "message": "session moved",
                    "details": {"authorization": "secret"},
                },
                headers={"location": "/redirected/session"},
            )
        if request.url.path == "/redirected/session":
            return httpx.Response(
                200,
                json={"session_id": "session-1", "stage": "selecting_quote"},
            )
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
        follow_redirects=True,
    ) as http:
        client = WalletAppClient.restore(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
            conversation_id="conversation-1",
            session_id="session-1",
        )
        with pytest.raises(EvaluationHttpError) as raised:
            await client.session()

    assert requested_paths == ["/v1/swap/session-1"]
    assert raised.value.code == "SESSION_REDIRECTED"
    assert raised.value.status_code == 307
    assert client.steps[0].operation == "session"
    assert client.steps[0].http_status == 307
    assert client.steps[0].evidence == {"authorization": "[REDACTED]"}


@pytest.mark.asyncio
async def test_wallet_app_client_records_turn_redirect_and_keeps_metadata():
    requested_paths = []

    async def handler(request):
        requested_paths.append(request.url.path)
        if request.url.path == "/v1/agent/turn":
            return httpx.Response(
                307,
                json={
                    "code": "TURN_REDIRECTED",
                    "message": "turn moved",
                    "details": {"password": "secret"},
                },
                headers={"location": "/redirected/turn"},
            )
        if request.url.path == "/redirected/turn":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "conversation_id": "conversation-1",
                    "session_id": "session-1",
                    "status": "running",
                },
            )
        if request.url.path == "/v1/agent/stream/run-1":
            return httpx.Response(
                200,
                content=b'event: done\ndata: {"stage":"complete"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
        follow_redirects=True,
    ) as http:
        client = WalletAppClient(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
            turn_metadata={"swap_request": {"amount": "1"}},
        )
        with pytest.raises(EvaluationHttpError) as raised:
            await client.turn("swap")

    assert requested_paths == ["/v1/agent/turn"]
    assert raised.value.code == "TURN_REDIRECTED"
    assert raised.value.status_code == 307
    assert client.turn_metadata == {"swap_request": {"amount": "1"}}
    assert client.steps[0].operation == "turn"
    assert client.steps[0].http_status == 307
    assert client.steps[0].evidence == {"password": "[REDACTED]"}


@pytest.mark.asyncio
async def test_wallet_app_client_records_stream_redirect_with_following_http_client():
    requested_paths = []

    async def handler(request):
        requested_paths.append(request.url.path)
        if request.url.path == "/v1/agent/turn":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "conversation_id": "conversation-1",
                    "session_id": "session-1",
                    "status": "running",
                },
            )
        if request.url.path == "/v1/agent/stream/run-1":
            return httpx.Response(
                307,
                json={
                    "code": "STREAM_REDIRECTED",
                    "message": "stream moved",
                    "details": {"client_secret": "secret"},
                },
                headers={"location": "/redirected/stream"},
            )
        if request.url.path == "/redirected/stream":
            return httpx.Response(
                200,
                content=b'event: done\ndata: {"stage":"complete"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
        follow_redirects=True,
    ) as http:
        client = WalletAppClient(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
        )
        with pytest.raises(EvaluationHttpError) as raised:
            await client.turn("swap")

    assert requested_paths == ["/v1/agent/turn", "/v1/agent/stream/run-1"]
    assert raised.value.code == "STREAM_REDIRECTED"
    assert raised.value.status_code == 307
    assert [step.operation for step in client.steps] == ["turn", "stream"]
    assert client.steps[1].http_status == 307
    assert client.steps[1].evidence == {"client_secret": "[REDACTED]"}


@pytest.mark.asyncio
async def test_wallet_app_client_maps_every_public_json_operation_and_records_steps():
    requests = []
    responses = {
        ("GET", "/v1/swap/session-1"): {
            "session_id": "session-1",
            "stage": "selecting_quote",
        },
        ("POST", "/v1/swap/session-1/select-quote"): {
            "session_id": "session-1",
            "stage": "awaiting_confirmation",
            "selected_quote": {"provider_reference": "quote-1"},
            "audit": {
                "signature": "secret",
                "data": "0x1234567890abcdef",
            },
        },
        ("POST", "/v1/swap/session-1/confirm"): {
            "session_id": "session-1",
            "stage": "approval_required",
            "approved": True,
        },
        ("POST", "/v1/swap/session-1/approve-broadcast"): {
            "session_id": "session-1",
            "stage": "approval_pending",
            "approval_tx_hash": "0x" + "a" * 64,
        },
        ("POST", "/v1/swap/session-1/continue"): {
            "session_id": "session-1",
            "stage": "swap_ready",
            "pending_transaction": {"to": "0x" + "2" * 40},
        },
        ("POST", "/v1/swap/session-1/broadcast"): {
            "session_id": "session-1",
            "stage": "broadcast_pending",
            "broadcast_tx_hash": "0x" + "b" * 64,
        },
        ("GET", "/v1/transactions/ETH/0xstatus"): {
            "chain": "ETH",
            "tx_hash": "0xstatus",
            "status": "confirmed",
        },
    }

    async def handler(request):
        body = json.loads(request.content) if request.content else None
        requests.append(
            (request.method, request.url.path, dict(request.url.params), body)
        )
        return httpx.Response(200, json=responses[(request.method, request.url.path)])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as http:
        client = WalletAppClient.restore(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
            conversation_id="conversation-1",
            session_id="session-1",
        )
        await client.session()
        selected = await client.select_quote("quote-1")
        await client.confirm(True)
        await client.submit_approval_hash("ETH", "0x" + "a" * 64)
        await client.continue_swap()
        await client.submit_swap_hash("ETH", "0x" + "b" * 64)
        await client.transaction_status("ETH", "0xstatus")

    assert selected["audit"]["signature"] == "secret"
    assert requests == [
        ("GET", "/v1/swap/session-1", {"user_id": "alice"}, None),
        (
            "POST",
            "/v1/swap/session-1/select-quote",
            {},
            {"user_id": "alice", "provider_reference": "quote-1"},
        ),
        (
            "POST",
            "/v1/swap/session-1/confirm",
            {},
            {"user_id": "alice", "approved": True},
        ),
        (
            "POST",
            "/v1/swap/session-1/approve-broadcast",
            {},
            {
                "user_id": "alice",
                "chain": "ETH",
                "approve_tx_hash": "0x" + "a" * 64,
            },
        ),
        (
            "POST",
            "/v1/swap/session-1/continue",
            {},
            {"user_id": "alice"},
        ),
        (
            "POST",
            "/v1/swap/session-1/broadcast",
            {},
            {
                "user_id": "alice",
                "chain": "ETH",
                "tx_hash": "0x" + "b" * 64,
            },
        ),
        (
            "GET",
            "/v1/transactions/ETH/0xstatus",
            {"user_id": "alice"},
            None,
        ),
    ]
    assert [step.operation for step in client.steps] == [
        "session",
        "select_quote",
        "confirm",
        "approve_broadcast",
        "continue",
        "broadcast",
        "transaction_status",
    ]
    assert [
        (step.stage_before, step.stage_after) for step in client.steps
    ] == [
        (None, "selecting_quote"),
        ("selecting_quote", "awaiting_confirmation"),
        ("awaiting_confirmation", "approval_required"),
        ("approval_required", "approval_pending"),
        ("approval_pending", "swap_ready"),
        ("swap_ready", "broadcast_pending"),
        ("broadcast_pending", "broadcast_pending"),
    ]
    assert all(step.http_status == 200 for step in client.steps)
    assert all(step.error_code is None for step in client.steps)
    assert client.steps[1].evidence["audit"] == {
        "signature": "[REDACTED]",
        "data": {"selector": "0x12345678", "length": 18},
    }


@pytest.mark.asyncio
async def test_wallet_app_client_turn_metadata_is_one_shot():
    posted = []
    turn_number = 0

    async def handler(request):
        nonlocal turn_number
        if request.url.path == "/v1/agent/turn":
            turn_number += 1
            posted.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "run_id": f"run-{turn_number}",
                    "conversation_id": "conversation-1",
                    "session_id": "session-1",
                    "status": "running",
                },
            )
        return httpx.Response(
            200,
            content=(
                b'event: update\ndata: {"event":"update","data":'
                + json.dumps({"stage": f"stage-{turn_number}"}).encode()
                + b"}\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as http:
        client = WalletAppClient(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
            turn_metadata={"swap_request": {"amount": "1"}},
        )
        await client.turn("swap")
        await client.turn("status")

    assert [body["metadata"] for body in posted] == [
        {"swap_request": {"amount": "1"}},
        {},
    ]
    assert [step.operation for step in client.steps] == [
        "turn",
        "stream",
        "turn",
        "stream",
    ]
    assert client.stage == "stage-2"


@pytest.mark.asyncio
async def test_wallet_app_client_uses_last_applicable_sse_stage_for_next_step():
    async def handler(request):
        if request.url.path == "/v1/agent/turn":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "conversation_id": "conversation-1",
                    "session_id": "session-1",
                    "status": "running",
                },
            )
        if request.url.path == "/v1/agent/stream/run-1":
            return httpx.Response(
                200,
                content=(
                    b'event: update\ndata: {"data":{"stage":"selecting_quote"}}\n\n'
                    b'event: progress\ndata: {"message":"checking quotes"}\n\n'
                    b'event: update\ndata: {"data":{"stage":"awaiting_confirmation"}}\n\n'
                    b'event: done\ndata: {"status":"complete"}\n\n'
                ),
                headers={"content-type": "text/event-stream"},
            )
        if request.url.path == "/v1/swap/session-1":
            return httpx.Response(
                200,
                json={"session_id": "session-1", "stage": "awaiting_confirmation"},
            )
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as http:
        client = WalletAppClient(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
        )
        await client.turn("swap")
        stage_after_stream = client.stage
        await client.session()

    assert stage_after_stream == "awaiting_confirmation"
    assert client.steps[1].stage_after == "awaiting_confirmation"
    assert client.steps[2].stage_before == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_wallet_app_client_records_turn_and_stream_public_errors():
    async def turn_error(_request):
        return httpx.Response(
            503,
            json={
                "code": "TURN_UNAVAILABLE",
                "message": "try later",
                "details": {"password": "secret"},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(turn_error), base_url="http://test"
    ) as http:
        turn_client = WalletAppClient(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
        )
        with pytest.raises(EvaluationHttpError) as turn_raised:
            await turn_client.turn("swap")

    assert turn_raised.value.code == "TURN_UNAVAILABLE"
    assert turn_client.steps[0].operation == "turn"
    assert turn_client.steps[0].evidence == {"password": "[REDACTED]"}

    async def stream_error(request):
        if request.url.path == "/v1/agent/turn":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "conversation_id": "conversation-1",
                    "session_id": "session-1",
                    "status": "running",
                },
            )
        return httpx.Response(
            404,
            json={
                "code": "RUN_NOT_FOUND",
                "message": "missing",
                "details": {"client_secret": "secret"},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(stream_error), base_url="http://test"
    ) as http:
        stream_client = WalletAppClient(
            http,
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
        )
        with pytest.raises(EvaluationHttpError) as stream_raised:
            await stream_client.turn("swap")

    assert stream_raised.value.code == "RUN_NOT_FOUND"
    assert [step.operation for step in stream_client.steps] == ["turn", "stream"]
    assert stream_client.steps[1].http_status == 404
    assert stream_client.steps[1].evidence == {"client_secret": "[REDACTED]"}


@pytest.mark.parametrize("private_field", ["graph", "app", "store", "checkpoint", "steps"])
def test_wallet_app_client_restore_rejects_private_or_old_client_fields(private_field):
    with pytest.raises(TypeError):
        WalletAppClient.restore(
            object(),
            user_id="alice",
            address="0x" + "1" * 40,
            chain="ETH",
            conversation_id="conversation-1",
            session_id="session-1",
            **{private_field: object()},
        )
