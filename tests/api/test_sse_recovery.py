import httpx
import pytest
from langgraph.types import Interrupt

from wallet_agent.api import create_app
from wallet_agent.persistence import InMemoryRunStore


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_sse_run_can_be_consumed_and_resumed_from_another_instance():
    shared_store = InMemoryRunStore()
    producer = create_app(graph=object(), run_store=shared_store)
    consumer = create_app(graph=object(), run_store=shared_store)

    await shared_store.create("run-shared", owner_id="alice")
    await shared_store.append("run-shared", {"event": "progress", "step": 1})
    await shared_store.append("run-shared", {"event": "progress", "step": 2})
    await shared_store.append("run-shared", {"event": "complete"})
    await shared_store.set_status("run-shared", "complete")

    async with client_for(producer) as producer_client, client_for(consumer) as consumer_client:
        initial = await consumer_client.get("/v1/agent/stream/run-shared")
        resumed_header = await producer_client.get(
            "/v1/agent/stream/run-shared",
            headers={"Last-Event-ID": "1"},
        )
        resumed_query = await consumer_client.get(
            "/v1/agent/stream/run-shared?last_event_id=2"
        )

    assert initial.text.count("id: ") == 3
    assert "id: 1\n" in initial.text
    assert "id: 2\n" in initial.text
    assert "id: 3\n" in initial.text
    assert "id: 1\n" not in resumed_header.text
    assert "id: 2\n" in resumed_header.text
    assert "id: 3\n" in resumed_header.text
    assert "id: 2\n" not in resumed_query.text
    assert "id: 3\n" in resumed_query.text


class JsonStrictRunStore(InMemoryRunStore):
    async def append(self, run_id, event):
        import json

        json.dumps(event)
        return await super().append(run_id, event)


@pytest.mark.asyncio
async def test_run_events_are_json_safe_before_persistent_storage():
    store = JsonStrictRunStore()
    app = create_app(run_store=store)
    await store.create("interrupt-run")

    event_id = await app.state.run_store.append(
        "interrupt-run",
        {
            "event": "action_required",
            "state": {"__interrupt__": [{"id": "already-public"}]},
        },
    )

    assert event_id == "1"


@pytest.mark.asyncio
async def test_graph_interrupt_is_serialized_before_run_store_append():
    class InterruptGraph:
        async def astream(self, _value, *, config, stream_mode):
            del config, stream_mode
            yield {"__interrupt__": (Interrupt(value={"approved": False}, id="i-1"),)}

        async def aget_state(self, _config):
            class Snapshot:
                values = {
                    "response": {"kind": "confirmation_required"},
                    "__interrupt__": (Interrupt(value={"approved": False}, id="i-1"),),
                }
                tasks = ()
                next = ("confirm",)

            return Snapshot()

    store = JsonStrictRunStore()
    app = create_app(graph=InterruptGraph(), run_store=store)

    async with client_for(app) as client:
        turn = await client.post(
            "/v1/agent/turn",
            json={"user_id": "alice", "message": "confirm"},
        )
        await app.state.runs[turn.json()["run_id"]]["task"]
        stream = await client.get(f"/v1/agent/stream/{turn.json()['run_id']}")

    assert "event: action_required" in stream.text
    assert '"id": "i-1"' in stream.text
    assert "AGENT_EXECUTION_ERROR" not in stream.text


class UnavailableRunStore(InMemoryRunStore):
    async def ready(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_readiness_fails_when_runtime_persistence_is_unavailable():
    app = create_app(graph=object(), run_store=UnavailableRunStore())

    async with client_for(app) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "PERSISTENCE_UNAVAILABLE"
