"""Minimal live-service smoke test for mobile/API integration."""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("WALLET_AGENT_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("WALLET_AGENT_TOKEN"))
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=10) as client:
        health = client.get("/health")
        health.raise_for_status()
        ready = client.get("/ready")
        ready.raise_for_status()
        payload = {"message": "Reply with a short greeting."}
        turn = client.post("/v1/agent/turn", headers=headers, json=payload)
        if turn.status_code >= 400:
            print(json.dumps(turn.json(), ensure_ascii=False), file=sys.stderr)
            return 1
        body = turn.json()
        stream = client.get(f"/v1/agent/stream/{body['run_id']}", headers=headers)
        stream.raise_for_status()
        if "event:" not in stream.text:
            print("SSE stream did not contain events", file=sys.stderr)
            return 1
    print("wallet-agent smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
