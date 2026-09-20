import httpx
import pytest
from langgraph.types import Command, Interrupt

from wallet_agent.api import create_app
from wallet_agent.domain.errors import ChainCapabilityUnavailable


async def client_for(*, graph=None, chain_registry=None):
    app = create_app(graph=graph, chain_registry=chain_registry)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return app, client


@pytest.mark.asyncio
async def test_health_and_readiness_endpoints():
    app, client = await client_for(graph=object())
    async with client:
        health = await client.get("/health")
        ready = await client.get("/ready")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_validation_and_signing_material_errors_use_stable_envelope():
    _app, client = await client_for()
    async with client:
        malformed = await client.post("/v1/agent/turn", json={"message": "hi"})
        secret = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "message": "hi",
                "metadata": {"private_key": "never-send"},
            },
        )
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "VALIDATION_ERROR"
    assert secret.status_code == 422
    assert secret.json()["code"] == "VALIDATION_ERROR"
    assert "detail" not in secret.json()


@pytest.mark.asyncio
async def test_sse_completion_and_missing_run_contract():
    app, client = await client_for()
    async with client:
        turn = await client.post(
            "/v1/agent/turn", json={"user_id": "alice", "message": "hello"}
        )
        await app.state.runs[turn.json()["run_id"]]["task"]
        stream = await client.get(f"/v1/agent/stream/{turn.json()['run_id']}")
        missing = await client.get("/v1/agent/stream/missing-run")
    assert "event: complete" in stream.text
    assert "RUN_NOT_FOUND" in missing.text
    assert "run not found" in missing.text.lower()


@pytest.mark.asyncio
async def test_sse_serializes_langgraph_interrupt_payload():
    app, client = await client_for()
    app.state.runs["interrupt-run"] = {
        "status": "awaiting_confirmation",
        "events": [
            {
                "event": "action_required",
                "state": {
                    "__interrupt__": (
                        Interrupt(value={"action": "swap", "status": "requested"}, id="i-1"),
                    )
                },
            }
        ],
    }

    async with client:
        stream = await client.get("/v1/agent/stream/interrupt-run")

    assert stream.status_code == 200
    assert "event: action_required" in stream.text
    assert '"id": "i-1"' in stream.text
    assert '"action": "swap"' in stream.text


@pytest.mark.asyncio
async def test_run_graph_uses_async_state_snapshot_when_available():
    class AsyncSnapshotGraph:
        async def astream(self, value, *, config, stream_mode):
            yield {"response": {"kind": "clarification"}}

        async def aget_state(self, _config):
            class Snapshot:
                values = {"response": {"kind": "clarification"}}
                tasks = ()
                next = ()

            return Snapshot()

        def get_state(self, _config):
            raise AssertionError("synchronous checkpoint access must not be used")

    app, client = await client_for(graph=AsyncSnapshotGraph())
    async with client:
        turn = await client.post(
            "/v1/agent/turn", json={"user_id": "alice", "message": "hello"}
        )
        await app.state.runs[turn.json()["run_id"]]["task"]
        stream = await client.get(f"/v1/agent/stream/{turn.json()['run_id']}")

    assert "event: complete" in stream.text
    assert "event: error" not in stream.text


@pytest.mark.asyncio
async def test_new_message_resumes_pending_graph_interrupt_with_request_context():
    class PendingGraph:
        def __init__(self):
            self.inputs = []

        async def astream(self, value, *, config, stream_mode):
            del config, stream_mode
            self.inputs.append(value)
            yield {"response": {"kind": "clarification"}}

        async def aget_state(self, _config):
            class Snapshot:
                values = {"response": {"kind": "clarification"}}
                tasks = ()
                next = ()

            if not self.inputs:
                Snapshot.tasks = (
                    type(
                        "Task",
                        (),
                        {
                            "interrupts": (
                                type(
                                    "Interrupt",
                                    (),
                                    {"value": {"kind": "confirmation_required"}},
                                )(),
                            )
                        },
                    )(),
                )
                Snapshot.next = ("confirmation_wait",)
            return Snapshot()

    graph = PendingGraph()
    app, client = await client_for(graph=graph)
    async with client:
        turn = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "conversation_id": "pending-thread",
                "message": "改成 1 USDC 换 USDT",
            },
        )
        await app.state.runs[turn.json()["run_id"]]["task"]

    assert isinstance(graph.inputs[0], Command)
    assert graph.inputs[0].resume["request"]["message"] == "改成 1 USDC 换 USDT"


@pytest.mark.asyncio
async def test_new_message_does_not_resume_status_poll_task():
    class StatusPollGraph:
        def __init__(self):
            self.inputs = []

        async def astream(self, value, *, config, stream_mode):
            del config, stream_mode
            self.inputs.append(value)
            yield {"response": {"kind": "swap_status"}}

        async def aget_state(self, _config):
            class Snapshot:
                values = {"response": {"kind": "swap_status"}}
                tasks = (type("Task", (), {"interrupts": ()})(),)
                next = ("status_poll",)

            return Snapshot()

    graph = StatusPollGraph()
    app, client = await client_for(graph=graph)
    async with client:
        turn = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "conversation_id": "status-thread",
                "message": "怎么样了？",
            },
        )
        await app.state.runs[turn.json()["run_id"]]["task"]

    assert graph.inputs
    assert not isinstance(graph.inputs[0], Command)


@pytest.mark.asyncio
async def test_approval_interrupt_requires_approval_tx_hash_before_resume():
    class ApprovalGraph:
        def __init__(self):
            self.inputs = []

        async def astream(self, value, *, config, stream_mode):
            del config, stream_mode
            self.inputs.append(value)
            yield {"response": {"kind": "swap_status"}}

        async def aget_state(self, _config):
            class Snapshot:
                values = {"response": {"kind": "approval_required"}}
                tasks = (
                    type(
                        "Task",
                        (),
                        {
                            "interrupts": (
                                type(
                                    "Interrupt",
                                    (),
                                    {"value": {"kind": "approval_required"}},
                                )(),
                            )
                        },
                    )(),
                )
                next = ("swap_allowance",)

            return Snapshot()

    graph = ApprovalGraph()
    app, client = await client_for(graph=graph)
    async with client:
        turn = await client.post(
            "/v1/agent/turn",
            json={"user_id": "alice", "conversation_id": "approval-thread", "message": "继续"},
        )
        await app.state.runs[turn.json()["run_id"]]["task"]

    assert graph.inputs == []
    errors = [
        event
        for event in app.state.runs[turn.json()["run_id"]]["events"]
        if event["event"] == "error"
    ]
    assert errors
    assert errors[-1]["error"]["code"] == "GRAPH_INTERRUPT_CONFLICT"


@pytest.mark.asyncio
async def test_unknown_interrupt_does_not_mutate_graph_checkpoint():
    class UnknownGraph:
        def __init__(self):
            self.invoked = False

        async def astream(self, _value, *, config, stream_mode):
            del config, stream_mode
            self.invoked = True
            yield {"response": {"kind": "unexpected"}}

        async def aget_state(self, _config):
            class Snapshot:
                values = {}
                tasks = (
                    type(
                        "Task",
                        (),
                        {
                            "interrupts": (
                                type(
                                    "Interrupt",
                                    (),
                                    {"value": {"kind": "new_interrupt_kind"}},
                                )(),
                            )
                        },
                    )(),
                )
                next = ("unknown",)

            return Snapshot()

    graph = UnknownGraph()
    app, client = await client_for(graph=graph)
    async with client:
        turn = await client.post(
            "/v1/agent/turn",
            json={"user_id": "alice", "conversation_id": "unknown-thread", "message": "继续"},
        )
        await app.state.runs[turn.json()["run_id"]]["task"]

    assert graph.invoked is False
    errors = [
        event
        for event in app.state.runs[turn.json()["run_id"]]["events"]
        if event["event"] == "error"
    ]
    assert errors[-1]["error"]["code"] == "GRAPH_INTERRUPT_CONFLICT"


@pytest.mark.asyncio
async def test_turn_forwards_public_wallet_context_and_reuses_session_id():
    class CapturingGraph:
        def __init__(self):
            self.inputs = []

        async def astream(self, value, *, config, stream_mode):
            self.inputs.append((value, config, stream_mode))
            yield {"response": {"kind": "clarification"}}

        def get_state(self, _config):
            class Snapshot:
                values = {"response": {"kind": "clarification"}}
                tasks = ()
                next = ()

            return Snapshot()

    graph = CapturingGraph()
    app, client = await client_for(graph=graph)
    async with client:
        first = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "message": "把 1 USDC 换成 USDT",
                "address": "0x" + "1" * 40,
                "chain": "BASE",
            },
        )
        assert first.status_code == 200
        await app.state.runs[first.json()["run_id"]]["task"]
        second = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "conversation_id": first.json()["conversation_id"],
                "session_id": first.json()["session_id"],
                "message": "源 token 地址是 0x2222222222222222222222222222222222222222",
                "address": "0x" + "1" * 40,
                "chain": "BASE",
            },
        )
        assert second.status_code == 200
        await app.state.runs[second.json()["run_id"]]["task"]
    assert graph.inputs[0][0]["wallet_context"] == {
        "address": "0x" + "1" * 40,
        "chain": "BASE",
    }
    assert second.json()["session_id"] == first.json()["session_id"]


@pytest.mark.asyncio
async def test_automatic_swap_prepare_continuation_preserves_active_task():
    class QuoteGraph:
        def __init__(self):
            self.inputs = []
            self.values = {}

        async def astream(self, value, *, config, stream_mode):
            del config, stream_mode
            self.inputs.append(value)
            if len(self.inputs) == 1:
                asset = {
                    "chain": "BASE",
                    "chain_id": 8453,
                    "symbol": "USDC",
                    "decimals": 6,
                    "address": "0x" + "3" * 40,
                }
                quote = {
                    "provider": "bridgers",
                    "source_asset": asset,
                    "destination_asset": {**asset, "symbol": "USDT", "address": "0x" + "4" * 40},
                    "input_amount": "1",
                    "input_amount_raw": "1000000",
                    "expected_output": "0.99",
                    "expected_output_raw": "990000",
                    "provider_reference": "quote-1",
                }
                self.values = {
                    "request": value["request"],
                    "response": {"kind": "swap_quote", "quotes": [quote]},
                    "selected_quote": quote,
                    "quote_candidates": [quote],
                    "swap_request": {
                        "source_asset": asset,
                        "destination_asset": quote["destination_asset"],
                        "input_amount": "1",
                        "input_amount_raw": "1000000",
                        "sender_address": "0x" + "1" * 40,
                        "recipient_address": "0x" + "1" * 40,
                    },
                    "active_task": {
                        "task_id": "swap-task",
                        "kind": "swap",
                        "status": "ready",
                        "stage": "ready_for_quote",
                        "revision": 1,
                        "slots": {"input_amount": "1"},
                        "slot_sources": {"input_amount": "user"},
                        "missing_fields": [],
                    },
                }
            else:
                self.values = {**self.values, "response": {"kind": "confirmation_required"}}
            yield {"response": self.values["response"]}

        def get_state(self, _config):
            values = self.values

            class Snapshot:
                tasks = ()
                next = ()

            snapshot = Snapshot()
            snapshot.values = values
            return snapshot

    graph = QuoteGraph()
    app, client = await client_for(graph=graph)
    async with client:
        response = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "message": "在 Base 用 1 USDC 换 USDT",
                "address": "0x" + "1" * 40,
                "chain": "BASE",
            },
        )
        await app.state.runs[response.json()["run_id"]]["task"]

    assert graph.inputs[1]["active_task"]["task_id"] == "swap-task"
    assert graph.inputs[1]["active_task"]["revision"] == 1


@pytest.mark.asyncio
async def test_failed_swap_quote_session_is_not_marked_completed():
    class FailedQuoteGraph:
        async def astream(self, value, *, config, stream_mode):
            del config, stream_mode
            self.value = value
            yield {"response": {"kind": "error"}}

        def get_state(self, _config):
            class Snapshot:
                values = {
                    "intent": "swap_quote",
                    "response": {
                        "kind": "error",
                        "errors": [{"code": "INVALID_QUOTE", "message": "quote failed"}],
                    },
                    "selected_quote": None,
                }
                tasks = ()
                next = ()

            return Snapshot()

    graph = FailedQuoteGraph()
    app, client = await client_for(graph=graph)
    async with client:
        response = await client.post(
            "/v1/agent/turn",
            json={"user_id": "alice", "message": "我想换 5USDT"},
        )
        await app.state.runs[response.json()["run_id"]]["task"]
        session = await client.get(
            f"/v1/swap/{response.json()['session_id']}", params={"user_id": "alice"}
        )

    assert session.status_code == 200
    assert session.json()["status"] == "quote_failed"


@pytest.mark.asyncio
async def test_unsupported_chain_error_is_stable():
    class Registry:
        def get_adapter(self, chain):
            raise ChainCapabilityUnavailable(chain, "balances")

    _app, client = await client_for(chain_registry=Registry())
    async with client:
        response = await client.get("/v1/wallet/address/balances", params={"chain": "SUI"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "CHAIN_CAPABILITY_UNAVAILABLE"
    assert body["details"]["chain"] == "SUI"


@pytest.mark.asyncio
async def test_transaction_status_endpoint_returns_user_readable_status():
    class Adapter:
        async def get_transaction_status(self, _tx_hash):
            from wallet_agent.domain.models import TransactionStatus

            return TransactionStatus.CONFIRMED

        async def get_transaction_receipt(self, _tx_hash):
            return {"status": "0x1", "blockNumber": "0x10"}

    class Registry:
        def get_adapter(self, _chain):
            return Adapter()

    _app, client = await client_for(chain_registry=Registry())
    tx_hash = "0x" + "a" * 64
    async with client:
        response = await client.get(f"/v1/transactions/BASE/{tx_hash}")
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    assert response.json()["message"] == "交易已确认。"
    assert response.json()["receipt"]["status"] == "0x1"


@pytest.mark.asyncio
async def test_transaction_status_endpoint_rejects_invalid_hash():
    _app, client = await client_for()
    async with client:
        response = await client.get("/v1/transactions/BASE/not-a-hash")
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_TRANSACTION_HASH"


@pytest.mark.asyncio
async def test_turn_accepts_explicit_transaction_query():
    class CapturingGraph:
        def __init__(self):
            self.input = None

        async def astream(self, value, *, config, stream_mode):
            self.input = value
            yield {"response": {"kind": "transaction_status"}}

        def get_state(self, _config):
            class Snapshot:
                values = {"response": {"kind": "transaction_status"}}
                tasks = ()
                next = ()

            return Snapshot()

    graph = CapturingGraph()
    app, client = await client_for(graph=graph)
    async with client:
        response = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "message": "查这笔交易",
                "metadata": {
                    "transaction_query": {
                        "chain": "BASE",
                        "tx_hash": "0x" + "a" * 64,
                    }
                },
            },
        )
        await app.state.runs[response.json()["run_id"]]["task"]
    assert response.status_code == 200
    assert graph.input["intent"] == "transaction_status"
    assert graph.input["transaction_query"]["chain"] == "BASE"


@pytest.mark.asyncio
async def test_turn_accepts_only_supported_agent_test_intents():
    class CapturingGraph:
        def __init__(self):
            self.input = None

        async def astream(self, value, *, config, stream_mode):
            self.input = value
            yield {"response": {"kind": "unsupported"}}

        def get_state(self, _config):
            class Snapshot:
                values = {"response": {"kind": "unsupported"}}
                tasks = ()
                next = ()

            return Snapshot()

    graph = CapturingGraph()
    app, client = await client_for(graph=graph)
    async with client:
        accepted = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "message": "调试不支持分支",
                "metadata": {"agent_test_intent": "unsupported"},
            },
        )
        await app.state.runs[accepted.json()["run_id"]]["task"]
        rejected = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "message": "调试未知分支",
                "metadata": {"agent_test_intent": "not-an-intent"},
            },
        )
        malformed = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "message": "调试非法分支",
                "metadata": {"agent_test_intent": ["unsupported"]},
            },
        )

    assert accepted.status_code == 200
    assert graph.input["intent"] == "unsupported"
    assert graph.input["forced_intent"] == "unsupported"
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "INVALID_AGENT_TEST_INTENT"
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "INVALID_AGENT_TEST_INTENT"


@pytest.mark.asyncio
async def test_portfolio_and_gas_endpoints_return_structured_results():
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, FeeEstimate, TokenBalance, TokenPrice

    class Adapter:
        async def get_native_balance(self, _address):
            asset = Asset(chain="BASE", symbol="ETH", decimals=18)
            return TokenBalance(asset=asset, amount=Decimal("1"), amount_raw="100")

        async def get_token_balances(self, _address):
            return []

        async def estimate_fee(self, *, to=None, data=None):
            asset = Asset(chain="BASE", symbol="ETH", decimals=18)
            return FeeEstimate(
                chain="BASE",
                asset=asset,
                amount=Decimal("0.5"),
                amount_raw="50",
            )

    class Registry:
        def get_adapter(self, _chain):
            return Adapter()

    class Prices:
        async def get_prices(self, assets):
            return [TokenPrice(asset=asset, usd_price=Decimal("2")) for asset in assets]

    app, client = await client_for(chain_registry=Registry())
    app.state.price_provider = Prices()
    address = "0x" + "1" * 40
    async with client:
        portfolio = await client.get(f"/v1/wallet/{address}/portfolio", params={"chain": "BASE"})
        gas = await client.get(f"/v1/wallet/{address}/gas", params={"chain": "BASE"})
    assert portfolio.status_code == 200
    assert portfolio.json()["price_status"] == "available"
    assert portfolio.json()["total_usd_value"] == "2"
    assert gas.status_code == 200
    assert gas.json()["sufficient"] is True
    assert gas.json()["shortfall_raw"] == "0"


@pytest.mark.asyncio
async def test_assets_endpoint_filters_provider_and_deduplicates():
    from wallet_agent.domain.models import Asset

    class Provider:
        async def list_assets(self, _query):
            return [
                Asset(chain="BASE", symbol="USDC", decimals=6, address="0x" + "3" * 40),
                Asset(chain="BASE", symbol="USDC", decimals=6, address="0x" + "3" * 40),
            ]

    app, client = await client_for()
    app.state.providers = {"bridgers": Provider()}
    async with client:
        response = await client.get(
            "/v1/assets",
            params={"chain": "BASE", "search": "USDC", "provider": "bridgers"},
        )
    assert response.status_code == 200
    assert len(response.json()["assets"]) == 1
    assert response.json()["assets"][0]["address"] == "0x" + "3" * 40
