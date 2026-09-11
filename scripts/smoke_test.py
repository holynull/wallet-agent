"""Minimal live-service smoke test for mobile/API integration."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        event = "message"
        data = ""
        for line in frame.splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data += line.removeprefix("data: ")
        try:
            parsed = json.loads(data) if data else {}
        except json.JSONDecodeError:
            parsed = {"raw": data}
        events.append({"event": event, "data": parsed})
    return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("WALLET_AGENT_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("WALLET_AGENT_TOKEN"))
    parser.add_argument("--user-id", default=os.getenv("WALLET_AGENT_USER_ID", "smoke-user"))
    parser.add_argument("--message", default="你好，联调检查")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        timeout=args.timeout,
        trust_env=False,
    ) as client:
        health = client.get("/health")
        health.raise_for_status()
        print(f"health: {health.status_code} {health.text}")
        ready = client.get("/ready")
        ready.raise_for_status()
        print(f"ready: {ready.status_code} {ready.text}")
        payload = {"user_id": args.user_id, "message": args.message, "metadata": {}}
        turn = client.post("/v1/agent/turn", headers=headers, json=payload)
        if turn.status_code >= 400:
            print(json.dumps(turn.json(), ensure_ascii=False), file=sys.stderr)
            return 1
        body = turn.json()
        print(f"turn: {turn.status_code} {json.dumps(body, ensure_ascii=False)}")
        stream = client.get(f"/v1/agent/stream/{body['run_id']}", headers=headers)
        stream.raise_for_status()
        events = _parse_sse(stream.text)
        for item in events:
            print(f"sse: {item['event']} {json.dumps(item['data'], ensure_ascii=False)}")
        event_names = {item["event"] for item in events}
        if not events:
            print("SSE stream did not contain events", file=sys.stderr)
            return 1
        if "error" in event_names:
            print("SSE stream returned an error event", file=sys.stderr)
            return 1
        if not (event_names & {"complete", "action_required"}):
            print("SSE stream did not reach a terminal event", file=sys.stderr)
            return 1
    print("wallet-agent smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
