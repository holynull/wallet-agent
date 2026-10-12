"""SSE frames carry the App's data, not the agent's internals.

The App streams a turn while a human reads the log, so every frame it receives
should be something it renders: the LangChain message list (system prompt, token
usage, raw tool payloads), the echoed request and the internal bookkeeping are
pure noise there — and the system prompt is not the App's business either.
"""

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from wallet_agent.api import create_app
from wallet_agent.graph.build import build_graph

SYSTEM_PROMPT_FRAGMENT = "你是多链数字货币钱包助手"
EVM = "0x3f847a21bcfbba535a5e2a1611bb4d545541d2a5"
TERMINAL_EVENTS = {"complete", "error", "action_required"}


class ReActChipModel:
    """Asks for the transfer chips, then answers — enough to produce every
    ReAct node update (react_entry / react_agent / react_tools)."""

    async def invoke_react(self, messages, tools):
        tool_messages = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[{"id": "qr-1", "name": "transfer_quick_replies", "args": {}}],
            )
        return AIMessage(content="转给谁？给我个地址 👇")


async def _turn_frames() -> list[tuple[str, str]]:
    graph = build_graph(model=ReActChipModel())
    app = create_app(graph=graph)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        turn = await client.post(
            "/v1/agent/turn",
            json={
                "conversation_id": "stream-payload",
                "user_id": "alice",
                "message": "转一笔帐给朋友",
                "address": EVM,
                "chain": "BASE",
                "wallet_accounts": [{"chain": "BASE", "address": EVM}],
            },
        )
        run_id = turn.json()["run_id"]
        frames: list[tuple[str, str]] = []
        async with client.stream("GET", f"/v1/agent/stream/{run_id}") as stream:
            kind = None
            async for line in stream.aiter_lines():
                if line.startswith("event: "):
                    kind = line[len("event: ") :]
                elif line.startswith("data: "):
                    frames.append((kind or "?", line[len("data: ") :]))
                    if kind in TERMINAL_EVENTS:
                        break
    return frames


@pytest.mark.asyncio
async def test_stream_never_ships_the_agent_message_list_or_system_prompt():
    frames = await _turn_frames()
    import json

    assert frames, "the turn produced no frames"
    assert frames[-1][0] == "complete"
    payloads = [json.loads(body) for _, body in frames]
    bodies = "\n".join(body for _, body in frames)

    assert SYSTEM_PROMPT_FRAGMENT not in bodies
    for payload in payloads:
        for container in (payload.get("state"), payload.get("data")):
            assert not isinstance(container, dict) or "messages" not in container
            assert not isinstance(container, dict) or "request" not in container


@pytest.mark.asyncio
async def test_stream_keeps_the_app_facing_projection_and_trims_history():
    import json

    frames = await _turn_frames()
    state = json.loads(frames[-1][1]).get("state") or {}

    # The chips the App renders are still there, with their type/value contract.
    suggestions = state["response"]["suggestions"]
    assert [item["value"] for item in suggestions] == [
        "input_address",
        "pick_contact",
        "show_balance",
    ]
    assert state["response"]["kind"] == "react"
    history = state.get("conversation_history")
    assert history, "the assistant turn should still be visible in history"
    assert all(
        set(item) <= {"role", "content", "id", "suggestions"} for item in history
    )
