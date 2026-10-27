from decimal import Decimal

import httpx
import pytest
from langgraph.types import Command, Interrupt

from wallet_agent.api import create_app
from wallet_agent.api.app import _assistant_history_entry, _conversation_artifacts, _stream_state
from wallet_agent.domain.models import (
    Asset,
    NormalizedOrderStatus,
    NormalizedQuote,
    ProviderOrder,
    UnsignedTransaction,
)
from wallet_agent.persistence import InMemorySessionStore, SwapSessionRecord


async def client_for(*, graph=None, chain_registry=None, **kwargs):
    app = create_app(graph=graph, chain_registry=chain_registry, **kwargs)
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


def test_conversation_artifacts_are_scoped_to_the_current_response():
    state = {
        "quote_candidates": [{"provider_reference": "stale-quote"}],
        "approval_transaction": {"to": "0xapprove"},
        "pending_transaction": {"to": "0xswap"},
        "response": {"kind": "swap_status", "status": {"status": "pending"}},
    }

    artifacts = _conversation_artifacts(state)

    assert artifacts == {
        "response": {"kind": "swap_status", "status": {"status": "pending"}},
        "status": {"kind": "swap_status", "status": {"status": "pending"}},
    }
    assert _conversation_artifacts(
        {
            "authorization_stage": "swap_ready",
            "pending_transaction": {"to": "0xstale"},
            "response": {
                "kind": "clarification",
                "errors": [{"code": "MISSING_PRICE_PARAMETERS"}],
            },
        }
    ) is None


def test_assistant_history_entry_persists_card_snapshot():
    state = {
        "response": {
            "kind": "swap_quote",
            "quotes": [{"provider": "bridgers", "provider_reference": "quote-1"}],
        }
    }

    entry = _assistant_history_entry(state)

    assert entry["content"] == "我找到了 1 个兑换报价，请比较后选择一个 Provider。"
    assert entry["artifacts"]["quotes"][0]["provider_reference"] == "quote-1"


def test_assistant_history_entry_persists_read_only_trace():
    entry = _assistant_history_entry(
        {
            "response": {"kind": "price_query", "prices": [{"usd_price": "1800"}]},
            "read_only_enabled": True,
            "read_loop_count": 1,
            "read_tool_calls": [{"name": "price_lookup", "status": "completed"}],
            "read_facts": {"price_lookup": {"ok": True}},
            "read_reference": {"kind": "asset", "id": "ETH:ETH"},
        }
    )

    assert entry["artifacts"]["read_only"] == {
        "enabled": True,
        "loop_count": 1,
        "tool_calls": [{"name": "price_lookup", "status": "completed"}],
        "facts": {"price_lookup": {"ok": True}},
        "reference": {"kind": "asset", "id": "ETH:ETH"},
    }


def test_react_response_persists_current_turn_tool_results_only():
    artifacts = _conversation_artifacts(
        {
            "response": {
                "kind": "react",
                "message": "ETH 当前价格约为 1800 美元。",
                "react_results": [
                    {"tool": "price_lookup", "result": {"ok": True, "usd_price": "1800"}},
                ],
            },
            "model_trace": [{"role": "react_agent", "model_id": "deepseek-chat"}],
        }
    )

    assert artifacts == {
        "response": {
            "kind": "react",
            "message": "ETH 当前价格约为 1800 美元。",
            "react_results": [
                {"tool": "price_lookup", "result": {"ok": True, "usd_price": "1800"}},
            ],
        },
        "model_trace": [{"role": "react_agent", "model_id": "deepseek-chat"}],
        "react_results": [
            {"tool": "price_lookup", "result": {"ok": True, "usd_price": "1800"}},
        ],
    }


def test_react_response_persists_route_and_tool_timing_metadata():
    artifacts = _conversation_artifacts(
        {
            "response": {"kind": "react", "message": "已查询。"},
            "graph_role": "conversation",
            "route_mode": "react",
            "route_reason": "model_answer",
            "react_tool_timings": [
                {
                    "call_id": "price-1",
                    "tool": "price_lookup",
                    "elapsed_ms": 12.4,
                    "status": "success",
                    "error_code": None,
                }
            ],
        }
    )

    assert artifacts["response"]["route"] == {
        "graph_role": "conversation",
        "route_mode": "react",
        "route_reason": "model_answer",
    }
    assert artifacts["response"]["react_tool_timings"][0]["tool"] == "price_lookup"


@pytest.mark.asyncio
async def test_debug_metrics_endpoint_returns_safe_aggregates():
    app, client = await client_for()
    app.state.metrics = type(
        "Metrics",
        (),
        {"snapshot": lambda _self: {"model_calls": 1, "tool_calls": 2}},
    )()
    app.state.metadata_cache = type(
        "Cache",
        (),
        {"stats": lambda _self: {"hits": 1, "misses": 2}},
    )()

    async with client:
        response = await client.get("/debug/metrics")

    assert response.status_code == 200
    assert response.json() == {
        "metrics": {"model_calls": 1, "tool_calls": 2},
        "metadata_cache": {"hits": 1, "misses": 2},
    }


def test_sse_state_does_not_duplicate_persisted_card_payloads():
    state = {
        "response": {"kind": "swap_quote", "quotes": []},
        "conversation_history": [
            {
                "role": "assistant",
                "content": "报价",
                "artifacts": {"response": {"kind": "swap_quote"}, "quotes": []},
            }
        ],
    }

    public = _stream_state(state)

    assert public["conversation_history"] == [{"role": "assistant", "content": "报价"}]
    assert public["response"] == state["response"]


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
async def test_new_transfer_projection_clears_stale_swap_session_lifecycle():
    session_store = InMemorySessionStore()
    await session_store.save(
        SwapSessionRecord(
            session_id="session-transfer-reset",
            user_id="alice",
            thread_id="conversation-transfer-reset",
            status="completed",
            stage="completed",
            provider_order=ProviderOrder(
                provider="bridgers",
                provider_order_id="old-order",
                provider_reference="old-reference",
                tx_hash="0x" + "a" * 64,
            ),
            broadcast_tx_hash="0x" + "a" * 64,
            broadcast_status="confirmed",
        )
    )

    class Graph:
        def __init__(self):
            self.state = None

        async def astream(self, value, *, config, stream_mode):
            del config, stream_mode
            self.state = {
                **value,
                "intent": "clarification",
                "operation_reset": True,
                "active_task": {
                    "task_id": "transfer-task",
                    "kind": "transfer",
                    "status": "collecting",
                    "stage": "collecting_parameters",
                    "revision": 1,
                    "slots": {"chain": "BSC", "symbol": "USDC"},
                    "slot_sources": {},
                    "missing_fields": ["transfer_recipient"],
                },
                "task_stage": "collecting_parameters",
                "provider_orders": {},
                "provider_order_ids": {},
                "selected_quote": None,
                "broadcast_tx_hash": None,
                "broadcast_status": None,
                "response": {
                    "kind": "clarification",
                    "message": "请补充收款地址。",
                    "missing_fields": ["transfer_recipient"],
                },
            }
            yield ("updates", {"response": self.state["response"]})

        async def aget_state(self, _config):
            class Snapshot:
                tasks = ()
                next = ()

            snapshot = Snapshot()
            snapshot.values = self.state or {}
            return snapshot

    graph = Graph()
    app = create_app(graph=graph, store=session_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "conversation_id": "conversation-transfer-reset",
                "session_id": "session-transfer-reset",
                "message": "给自己转 1 USDC",
            },
        )
        await app.state.runs[response.json()["run_id"]]["task"]

    assert response.status_code == 200
    projected = await session_store.get("session-transfer-reset")
    assert projected is not None
    assert projected.status == "collecting_parameters"
    assert projected.stage == "collecting_parameters"
    assert projected.provider_order is None
    assert projected.broadcast_tx_hash is None


@pytest.mark.asyncio
async def test_sse_forwards_custom_graph_progress_events():
    class ProgressGraph:
        async def astream(self, _value, *, config, stream_mode):
            del config
            assert stream_mode == ["updates", "custom"]
            yield ("custom", {"stage": "slot_extraction", "status": "started", "elapsed_ms": 0})
            yield ("updates", {"response": {"kind": "clarification"}})

        async def aget_state(self, _config):
            class Snapshot:
                values = {"response": {"kind": "clarification"}}
                tasks = ()
                next = ()

            return Snapshot()

    app, client = await client_for(graph=ProgressGraph())
    async with client:
        turn = await client.post("/v1/agent/turn", json={"user_id": "alice", "message": "hello"})
        await app.state.runs[turn.json()["run_id"]]["task"]
        stream = await client.get(f"/v1/agent/stream/{turn.json()['run_id']}")

    assert "event: progress" in stream.text
    assert '"stage": "slot_extraction"' in stream.text


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
async def test_app_networks_and_accounts_are_forwarded_and_explicit_empty_clears_scope():
    class Graph:
        inputs = []

        async def astream(self, value, *, config, stream_mode):
            self.inputs.append(value)
            yield {"response": {"kind": "react", "message": "余额"}}

        def get_state(self, _config):
            class Snapshot:
                values = {"response": {"kind": "react", "message": "余额"}}
                tasks = ()
                next = ()
            return Snapshot()

    graph = Graph()
    app, client = await client_for(graph=graph)
    accounts = [{"chain": "BTC", "address": "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"}]
    async with client:
        first = await client.post("/v1/agent/turn", json={"user_id": "alice", "message": "全部余额",
            "supported_networks": ["ETH", "BTC"], "wallet_accounts": accounts})
        assert first.status_code == 200
        await app.state.runs[first.json()["run_id"]]["task"]
        second = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "message": "余额",
                "conversation_id": first.json()["conversation_id"],
                "supported_networks": [],
                "wallet_accounts": [],
            },
        )
        assert second.status_code == 200
        await app.state.runs[second.json()["run_id"]]["task"]
        invalid = await client.post(
            "/v1/agent/turn",
            json={
                "message": "余额",
                "wallet_accounts": [{"chain": "BTC", "address": "a", "private_key": "secret"}],
            },
        )
        assert invalid.status_code == 422
    assert graph.inputs[0]["wallet_context"] == {
        "supported_networks": ["ETH", "BTC"],
        "wallet_accounts": accounts,
    }
    assert graph.inputs[1]["wallet_context"] == {"supported_networks": [], "wallet_accounts": []}


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
async def test_transfer_error_clears_stale_unsigned_transaction_from_session():
    stale_transaction = UnsignedTransaction(
        chain="BASE",
        chain_id=8453,
        to="0x" + "2" * 40,
        data="0x",
        value="1000000",
    )
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="stale-transfer-session",
            user_id="alice",
            thread_id="stale-transfer-thread",
            status="transfer_ready",
            stage="transfer_ready",
            pending_transaction=stale_transaction,
            approval_transaction={"to": "0x" + "3" * 40},
            confirmation_state={"status": "requested"},
        )
    )

    class FailedTransferGraph:
        async def astream(self, _value, *, config, stream_mode):
            del config, stream_mode
            yield {"response": {"kind": "error"}}

        def get_state(self, _config):
            class Snapshot:
                values = {
                    "intent": "transfer",
                    "active_task": {"kind": "transfer", "stage": "ready_for_prepare"},
                    "task_stage": "ready_for_prepare",
                    "preflight": {"ok": False},
                    "response": {
                        "kind": "error",
                        "errors": [{"code": "TRANSACTION_PREFLIGHT_FAILED"}],
                    },
                }
                tasks = ()
                next = ()

            return Snapshot()

    app, client = await client_for(graph=FailedTransferGraph(), store=store)
    async with client:
        response = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "session_id": "stale-transfer-session",
                "conversation_id": "stale-transfer-thread",
                "message": "转一些 ETH 到这个地址",
            },
        )
        await app.state.runs[response.json()["run_id"]]["task"]
        session = await client.get(
            "/v1/swap/stale-transfer-session", params={"user_id": "alice"}
        )

    assert session.status_code == 200
    body = session.json()
    assert body["status"] == "failed"
    assert body["stage"] == "failed"
    assert body["pending_transaction"] is None
    assert body["approval_transaction"] is None
    assert body["confirmation_state"] is None
    assert body["preflight"] == {"ok": False}


@pytest.mark.asyncio
async def test_new_transfer_prepare_clears_previous_broadcast_projection():
    old_hash = "0x" + "a" * 64
    quote = NormalizedQuote(
        provider="omnibridge",
        source_asset=Asset(
            chain="ETH",
            chain_id=1,
            symbol="USDT",
            decimals=6,
            address="0x" + "1" * 40,
        ),
        destination_asset=Asset(chain="BSC", chain_id=56, symbol="BNB", decimals=18),
        input_amount=Decimal("9"),
        input_amount_raw="9000000",
        expected_output=Decimal("0.01"),
        expected_output_raw="10000000000000000",
        provider_reference="swap-ref",
    )
    provider_order = ProviderOrder(
        provider="omnibridge",
        provider_order_id="order-1",
        provider_reference="swap-ref",
        tx_hash="0x" + "b" * 64,
    )
    order_status = NormalizedOrderStatus(
        provider="omnibridge",
        provider_order_id="order-1",
        provider_reference="swap-ref",
        status="completed",
    )
    new_transaction = UnsignedTransaction(
        chain="ETH",
        chain_id=1,
        to="0x" + "2" * 40,
        data="0x",
        value="100000000000000",
    )
    store = InMemorySessionStore()
    await store.save(
        SwapSessionRecord(
            session_id="new-transfer-session",
            user_id="alice",
            thread_id="new-transfer-thread",
            status="not_propagated",
            stage="not_propagated",
            broadcast_tx_hash=old_hash,
            broadcast_status="not_propagated",
            quote=quote,
            selected_provider_reference="swap-ref",
            provider_order=provider_order,
            order_status=order_status,
            pending_transaction=UnsignedTransaction(
                chain="ETH", chain_id=1, to="0x" + "3" * 40, data="0x", value="1"
            ),
        )
    )

    class TransferGraph:
        async def astream(self, _value, *, config, stream_mode):
            del config, stream_mode
            yield {"response": {"kind": "transfer_prepare"}}

        def get_state(self, _config):
            class Snapshot:
                values = {
                    "intent": "transfer",
                    "response": {
                        "kind": "transfer_prepare",
                        "pending_transaction": new_transaction.model_dump(mode="json"),
                    },
                    "pending_transaction": new_transaction.model_dump(mode="json"),
                    "preflight": {"ok": True},
                    "task_stage": "ready_for_prepare",
                }
                tasks = ()
                next = ()

            return Snapshot()

    app, client = await client_for(graph=TransferGraph(), store=store)
    async with client:
        response = await client.post(
            "/v1/agent/turn",
            json={
                "user_id": "alice",
                "session_id": "new-transfer-session",
                "conversation_id": "new-transfer-thread",
                "message": "转 0.0001 ETH",
            },
        )
        await app.state.runs[response.json()["run_id"]]["task"]
        session = await client.get(
            "/v1/swap/new-transfer-session", params={"user_id": "alice"}
        )

    assert session.status_code == 200
    body = session.json()
    assert body["status"] == "transfer_ready"
    assert body["stage"] == "transfer_ready"
    assert body["broadcast_tx_hash"] is None
    assert body["broadcast_status"] is None
    assert body["pending_transaction"]["value"] == "100000000000000"
    assert body["quote"] is None
    assert body["selected_provider_reference"] is None
    assert body["provider_order"] is None
    assert body["order_status"] is None


@pytest.mark.asyncio
async def test_parameter_clarification_projects_collecting_session_status():
    class ClarificationGraph:
        async def astream(self, _value, *, config, stream_mode):
            del config, stream_mode
            yield {"response": {"kind": "clarification"}}

        def get_state(self, _config):
            class Snapshot:
                values = {
                    "active_task": {
                        "kind": "transfer",
                        "stage": "collecting_parameters",
                    },
                    "task_stage": "collecting_parameters",
                    "response": {"kind": "clarification"},
                }
                tasks = ()
                next = ()

            return Snapshot()

    app, client = await client_for(graph=ClarificationGraph())
    async with client:
        response = await client.post(
            "/v1/agent/turn",
            json={"user_id": "alice", "message": "给地址转 1 USDC"},
        )
        await app.state.runs[response.json()["run_id"]]["task"]
        session = await client.get(
            f"/v1/swap/{response.json()['session_id']}", params={"user_id": "alice"}
        )

    assert session.status_code == 200
    assert session.json()["status"] == "collecting_parameters"
    assert session.json()["stage"] == "collecting_parameters"


@pytest.mark.asyncio
async def test_unsupported_chain_error_is_stable():
    _app, client = await client_for()
    async with client:
        response = await client.get("/v1/wallet/address/balances", params={"chain": "SUI"})
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "OKX_CAPABILITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_transaction_status_endpoint_returns_user_readable_status():
    from wallet_agent.domain.models import TransactionDetail, TransactionStatus

    class Explorer:
        async def get_transaction_detail(self, chain, tx_hash):
            return TransactionDetail(
                chain=chain,
                chain_id="8453",
                tx_hash=tx_hash,
                status=TransactionStatus.CONFIRMED,
                source="okx",
                history_kind="full",
                block_number=16,
                failure_reason="Out of gas",
                eta=None,
            )

    _app, client = await client_for(explorer_provider=Explorer())
    tx_hash = "0x" + "a" * 64
    async with client:
        response = await client.get(f"/v1/transactions/BASE/{tx_hash}")
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    assert response.json()["message"] == "交易已确认。"
    assert response.json()["source"] == "okx"
    assert response.json()["transaction"]["history_kind"] == "full"
    # The detail-page "解释失败原因" fields surface at the top level.
    assert response.json()["failure_reason"] == "Out of gas"
    assert "eta" in response.json()
    assert response.json()["eta"] is None


@pytest.mark.asyncio
async def test_transaction_status_failed_transfer_falls_back_to_session_asset():
    """A failed ERC20 transfer with no token layer in the explorer must render
    the session's intended moved asset (amount + symbol) instead of the native
    "0 ETH" row on the failed status card."""
    from wallet_agent.domain.models import TransactionDetail, TransactionStatus

    class Explorer:
        async def get_transaction_detail(self, chain, tx_hash):
            return TransactionDetail(
                chain=chain,
                chain_id="1",
                tx_hash=tx_hash,
                status=TransactionStatus.FAILED,
                source="okx",
                history_kind="full",
                value="0",
                symbol="ETH",
                method_id="0xa9059cbb",
            )

    tx_hash = "0x" + "a" * 64
    session_store = InMemorySessionStore()
    await session_store.save(
        SwapSessionRecord(
            session_id="session-failed-dai",
            user_id="alice",
            thread_id="conversation-failed-dai",
            broadcast_tx_hash=tx_hash,
            broadcast_status="failed",
            asset={
                "chain": "ETH",
                "symbol": "DAI",
                "address": "0x6b175474e89094c44da98b954eedeac495271d0f",
                "decimals": 18,
                "amount": "11.3426",
            },
        )
    )

    _app, client = await client_for(explorer_provider=Explorer(), store=session_store)
    async with client:
        response = await client.get(
            f"/v1/transactions/ETH/{tx_hash}", params={"user_id": "alice"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["amount"] == "11.3426"
    assert body["symbol"] == "DAI"


@pytest.mark.asyncio
async def test_transaction_status_confirmed_token_transfer_falls_back_to_session_asset():
    """A confirmed token transfer with no token layer in the explorer (e.g. a
    DAI self-transfer) must render the session's intended moved asset instead
    of the native "0 ETH" row on the success card."""
    from wallet_agent.domain.models import TransactionDetail, TransactionStatus

    class Explorer:
        async def get_transaction_detail(self, chain, tx_hash):
            return TransactionDetail(
                chain=chain,
                chain_id="1",
                tx_hash=tx_hash,
                status=TransactionStatus.CONFIRMED,
                source="okx",
                history_kind="full",
                value="0",
                symbol="ETH",
                method_id="0xa9059cbb",
            )

    tx_hash = "0x" + "a" * 64
    session_store = InMemorySessionStore()
    await session_store.save(
        SwapSessionRecord(
            session_id="session-confirmed-dai-self",
            user_id="alice",
            thread_id="conversation-confirmed-dai-self",
            broadcast_tx_hash=tx_hash,
            broadcast_status="confirmed",
            asset={
                "chain": "ETH",
                "symbol": "DAI",
                "address": "0x6b175474e89094c44da98b954eedeac495271d0f",
                "decimals": 18,
                "amount": "10.755859901486159764",
            },
        )
    )

    _app, client = await client_for(explorer_provider=Explorer(), store=session_store)
    async with client:
        response = await client.get(
            f"/v1/transactions/ETH/{tx_hash}", params={"user_id": "alice"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["amount"] == "10.755859901486159764"
    assert body["symbol"] == "DAI"
    # The icon must come from the session's intended asset, keyed by the
    # checksummed Trust Wallet directory (a lowercase copy would 404).
    assert body["logo_url"] == (
        "https://raw.githubusercontent.com/trustwallet/assets/master/"
        "blockchains/ethereum/assets/0x6B175474E89094C44Da98b954EedeAC495271d0F/logo.png"
    )
    assert body["chain_logo_url"].endswith("/blockchains/ethereum/info/logo.png")


@pytest.mark.asyncio
async def test_transaction_status_token_layer_without_icon_falls_back_to_session_logo():
    """A confirmed token transfer whose explorer response carries the token
    layer (amount + symbol) but no icon must still render the token's logo
    derived from the session's intended asset."""
    from wallet_agent.domain.models import TransactionDetail, TransactionStatus

    class Explorer:
        async def get_transaction_detail(self, chain, tx_hash):
            return TransactionDetail(
                chain=chain,
                chain_id="1",
                tx_hash=tx_hash,
                status=TransactionStatus.CONFIRMED,
                source="okx",
                history_kind="full",
                value="0",
                symbol="ETH",
                method_id="0xa9059cbb",
                token_transfers=[
                    {
                        "symbol": "USDC",
                        "amount": "42.5",
                        "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    }
                ],
            )

    tx_hash = "0x" + "e" * 64
    session_store = InMemorySessionStore()
    await session_store.save(
        SwapSessionRecord(
            session_id="session-usdc-success",
            user_id="alice",
            thread_id="conversation-usdc-success",
            broadcast_tx_hash=tx_hash,
            broadcast_status="confirmed",
            asset={
                "chain": "ETH",
                "symbol": "USDC",
                "address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                "decimals": 6,
                "amount": "42.5",
            },
        )
    )

    _app, client = await client_for(explorer_provider=Explorer(), store=session_store)
    async with client:
        response = await client.get(
            f"/v1/transactions/ETH/{tx_hash}", params={"user_id": "alice"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["amount"] == "42.5"
    assert body["symbol"] == "USDC"
    assert body["logo_url"] == (
        "https://raw.githubusercontent.com/trustwallet/assets/master/"
        "blockchains/ethereum/assets/0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48/logo.png"
    )


@pytest.mark.asyncio
async def test_transaction_status_failed_transfer_falls_back_without_user_id():
    """A client that queries GET /v1/transactions without ``user_id`` (the
    demo, and App calls without a Bearer token on AUTH_REQUIRED=false) must
    still get the session's moved asset on a failed token transfer instead of
    the native "0 ETH" row."""
    from wallet_agent.domain.models import TransactionDetail, TransactionStatus

    class Explorer:
        async def get_transaction_detail(self, chain, tx_hash):
            return TransactionDetail(
                chain=chain,
                chain_id="1",
                tx_hash=tx_hash,
                status=TransactionStatus.FAILED,
                source="okx",
                history_kind="full",
                value="0",
                symbol="ETH",
                method_id="0xa9059cbb",
            )

    tx_hash = "0x" + "b" * 64
    session_store = InMemorySessionStore()
    await session_store.save(
        SwapSessionRecord(
            session_id="session-failed-usdt",
            user_id="bob",
            thread_id="conversation-failed-usdt",
            broadcast_tx_hash=tx_hash,
            broadcast_status="failed",
            asset={
                "chain": "ETH",
                "symbol": "USDT",
                "address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
                "decimals": 6,
                "amount": "31.105638",
            },
        )
    )

    _app, client = await client_for(explorer_provider=Explorer(), store=session_store)
    async with client:
        # No user_id query param — the handler falls back to anonymous.
        response = await client.get(f"/v1/transactions/ETH/{tx_hash}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["amount"] == "31.105638"
    assert body["symbol"] == "USDT"
    assert body["logo_url"] == (
        "https://raw.githubusercontent.com/trustwallet/assets/master/"
        "blockchains/ethereum/assets/0xdAC17F958D2ee523a2206206994597C13D831ec7/logo.png"
    )


@pytest.mark.asyncio
async def test_transaction_status_no_match_without_user_id_returns_native_row():
    """Anonymous lookups only annotate a known broadcast hash; an unknown hash
    still renders the explorer's native row (value 0 / native symbol)."""
    from wallet_agent.domain.models import TransactionDetail, TransactionStatus

    class Explorer:
        async def get_transaction_detail(self, chain, tx_hash):
            return TransactionDetail(
                chain=chain,
                chain_id="1",
                tx_hash=tx_hash,
                status=TransactionStatus.FAILED,
                source="okx",
                history_kind="full",
                value="0",
                symbol="ETH",
            )

    _app, client = await client_for(explorer_provider=Explorer())
    async with client:
        response = await client.get(f"/v1/transactions/ETH/{'0x' + 'c' * 64}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["amount"] == "0"
    assert body["symbol"] == "ETH"


@pytest.mark.asyncio
async def test_transaction_status_endpoint_rejects_invalid_hash():
    _app, client = await client_for()
    async with client:
        response = await client.get("/v1/transactions/BASE/not-a-hash")
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_TRANSACTION_HASH"


@pytest.mark.asyncio
async def test_transaction_status_endpoint_accepts_a_bitcoin_txid():
    from wallet_agent.domain.models import TransactionDetail, TransactionStatus

    class Explorer:
        async def get_transaction_detail(self, chain, tx_hash):
            return TransactionDetail(
                chain=chain,
                chain_id=None,
                tx_hash=tx_hash,
                status=TransactionStatus.CONFIRMED,
                source="okx",
            )

    _app, client = await client_for(explorer_provider=Explorer())
    async with client:
        response = await client.get("/v1/transactions/BTC/" + "c" * 64)
        rejected = await client.get("/v1/transactions/BTC/not-a-bitcoin-txid")
    assert response.status_code == 200
    assert response.json()["chain"] == "BTC"
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_balances_endpoint_falls_back_to_the_chain_adapter_without_okx():
    from decimal import Decimal

    from wallet_agent.domain.models import Asset, TokenBalance

    class Adapter:
        async def get_native_balance(self, _address):
            return TokenBalance(
                asset=Asset(chain="BTC", symbol="BTC", decimals=8),
                amount=Decimal("0.015"),
                amount_raw="1500000",
            )

        async def get_token_balances(self, _address):
            return []

    class Registry:
        def get_adapter(self, _chain):
            return Adapter()

    _app, client = await client_for(chain_registry=Registry())
    async with client:
        response = await client.get(
            "/v1/wallet/bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5/balances?chain=BTC"
        )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "chain"
    assert body["native"]["amount_raw"] == "1500000"


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
async def test_turn_accepts_explicit_swap_query_without_forcing_swap_intent():
    """metadata.swap_query (order detail entry) must surface into graph state so
    the ReAct entry can scope prompt + toolset, but must NOT force the swap
    lifecycle intent (that would restart slot collection over an existing order)."""

    class CapturingGraph:
        def __init__(self):
            self.input = None

        async def astream(self, value, *, config, stream_mode):
            self.input = value
            yield {"response": {"kind": "react"}}

        def get_state(self, _config):
            class Snapshot:
                values = {"response": {"kind": "react"}}
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
                "message": "分析这笔订单的详情？",
                "metadata": {
                    "swap_query": {
                        "provider_order_id": "order-789",
                        "tx_hash": "0x" + "b" * 64,
                        "chain": "BSC",
                    }
                },
            },
        )
        await app.state.runs[response.json()["run_id"]]["task"]
    assert response.status_code == 200
    assert graph.input["swap_query"]["provider_order_id"] == "order-789"
    assert graph.input["swap_query"]["chain"] == "BSC"
    # No forced lifecycle intent: the order analysis stays on the ReAct path.
    assert graph.input.get("forced_intent") is None
    assert graph.input.get("intent") is None


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

    class WalletProvider:
        async def get_token_balances(self, _address, chain_indexes):
            assert chain_indexes == ["8453"]
            asset = Asset(chain="BASE", chain_id=8453, symbol="ETH", decimals=18)
            return [TokenBalance(asset=asset, amount=Decimal("1"), amount_raw="100")]

    class Prices:
        async def get_prices(self, assets):
            return [TokenPrice(asset=asset, usd_price=Decimal("2")) for asset in assets]

    app, client = await client_for(
        chain_registry=Registry(), wallet_provider=WalletProvider()
    )
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


@pytest.mark.asyncio
async def test_conversation_list_titles_paging_and_search():
    """History management: a readable title, backwards paging and a title search."""
    from wallet_agent.api import create_app
    from wallet_agent.persistence import InMemorySessionStore, SwapSessionRecord

    class Graph:
        async def ainvoke(self, *_args, **_kwargs):  # pragma: no cover - not used here
            raise AssertionError("not used")

    store = InMemorySessionStore()
    for index, title in enumerate(["查一下余额", "转给朋友", "解释失败原因"]):
        await store.save(
            SwapSessionRecord(
                session_id=f"s{index}",
                user_id="alice",
                thread_id=f"t{index}",
                title=title,
            )
        )
    app = create_app(graph=Graph(), store=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/v1/agent/conversations?user_id=alice&limit=2")
        assert first.status_code == 200
        body = first.json()
        assert [item["title"] for item in body["conversations"]] == [
            "解释失败原因",
            "转给朋友",
        ]
        assert body["next_cursor"] == body["conversations"][-1]["updated_at"]

        second = await client.get(
            f"/v1/agent/conversations?user_id=alice&limit=2&cursor={body['next_cursor']}"
        )
        assert [item["title"] for item in second.json()["conversations"]] == ["查一下余额"]
        assert second.json()["next_cursor"] is None

        found = await client.get("/v1/agent/conversations?user_id=alice&q=朋友")
        assert [item["title"] for item in found.json()["conversations"]] == ["转给朋友"]

        bad = await client.get("/v1/agent/conversations?user_id=alice&cursor=not-a-date")
        assert bad.status_code == 422

        renamed = await client.patch(
            "/v1/agent/conversations/t1?user_id=alice", json={"title": "给妈妈转账", "pinned": True}
        )
        assert renamed.status_code == 200
        assert renamed.json()["conversation"]["title"] == "给妈妈转账"
        assert renamed.json()["conversation"]["pinned"] is True

        missing = await client.patch(
            "/v1/agent/conversations/nope?user_id=alice", json={"pinned": True}
        )
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_a_turn_titles_the_new_conversation_with_the_first_message():
    class Graph:
        async def ainvoke(self, *_args, **_kwargs):
            return {"response": {"kind": "react", "message": "ok"}, "conversation_history": []}

    app, client = await client_for(graph=Graph())
    async with client:
        turn = await client.post(
            "/v1/agent/turn",
            json={"user_id": "alice", "message": "我最近有哪些交易？看看流水"},
        )
        assert turn.status_code == 200
        await app.state.runs[turn.json()["run_id"]]["task"]

        listed = await client.get("/v1/agent/conversations?user_id=alice")

    conversations = listed.json()["conversations"]
    assert len(conversations) == 1
    assert conversations[0]["title"] == "我最近有哪些交易？看看流水"
    assert conversations[0]["pinned"] is False
