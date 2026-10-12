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
_REACT_NODE_KEYS = {"react_entry", "react_agent", "react_tools"}


def _walk(value: object, prefix: str = ""):
    """Yield every ``(path, value)`` pair in a JSON-shaped object."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{prefix}/{key}", item
            yield from _walk(item, f"{prefix}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{prefix}[{index}]")


def _assert_no_agent_internals(payloads: list[dict]) -> None:
    """No LangChain message list and no system prompt, anywhere in a frame.

    The ReAct loop publishes its messages nested under node names, and the
    serialized body unicode-escapes the Chinese system prompt, so a substring
    or top-level-key check would miss the leak — walk the decoded payloads.
    """
    for payload in payloads:
        for container in (payload.get("state"), payload.get("data")):
            if not isinstance(container, dict):
                continue
            for path, item in _walk(container):
                key = path.rsplit("/", 1)[-1]
                assert key != "messages", f"LangChain message list at {path}"
                if isinstance(item, str) and SYSTEM_PROMPT_FRAGMENT in item:
                    raise AssertionError(
                        f"system prompt leaked into the stream at {path}"
                    )

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

    # The react loop's per-step node updates used to be the bulk of the leak:
    # each carried the whole LangChain message list (system prompt, token
    # usage, tool payloads) nested under react_entry / react_agent / react_tools.
    for kind, body in frames:
        payload = json.loads(body)
        data = payload.get("data")
        if isinstance(data, dict):
            assert _REACT_NODE_KEYS.isdisjoint(data), (
                f"react node internals shipped in an {kind} frame"
            )
    _assert_no_agent_internals(payloads)


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


@pytest.mark.asyncio
async def test_progress_frames_are_loading_text_not_a_second_card():
    """The card belongs to the ``response`` frame and the terminal state.

    It used to be published again as a custom ``wallet_agent_card`` event on
    every progress frame — a leftover from the LangGraph Server spike — which
    sent a 25-asset portfolio card three times per turn.
    """
    import json

    frames = await _turn_frames()
    for kind, body in frames:
        payload = json.loads(body)
        data = payload.get("data")
        # The deleted spike re-published every card as a custom event.
        if isinstance(data, dict):
            assert data.get("kind") != "wallet_agent_card"
        # Progress frames, when a path emits them, are loading text only.
        if kind == "progress":
            assert isinstance(data, dict)
            assert "response" not in data
