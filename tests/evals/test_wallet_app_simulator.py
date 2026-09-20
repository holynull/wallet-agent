import json

import pytest

from evals.wallet_app_simulator import (
    Eip1193Error,
    Eip1193WalletSimulator,
    FaultOutcome,
    FaultSequence,
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
