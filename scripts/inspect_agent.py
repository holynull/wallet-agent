#!/usr/bin/env python3
"""Inspect one Wallet Agent run, conversation, or session over the HTTP API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

from wallet_agent.api.inspection import sanitize_debug_value


def _get_json(
    client: httpx.Client,
    path: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> Any:
    response = client.get(path, headers=headers, params=params)
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise RuntimeError(f"GET {path} failed ({response.status_code}): {detail}")
    return sanitize_debug_value(response.json())


def _run_from_lookup(
    client: httpx.Client,
    *,
    headers: dict[str, str],
    params: dict[str, str],
    run_id: str | None,
) -> Any:
    if not run_id:
        return None
    return _get_json(
        client,
        f"/v1/agent/runs/{run_id}",
        headers=headers,
        params=params,
    )


def _format_scalar(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _print_run(run: dict[str, Any], *, show_events: bool) -> None:
    print("Run")
    print(f"  run_id:          {_format_scalar(run.get('run_id'))}")
    print(f"  status:          {_format_scalar(run.get('status'))}")
    print(f"  owner_id:        {_format_scalar(run.get('owner_id'))}")
    print(f"  conversation_id: {_format_scalar(run.get('conversation_id'))}")
    print(f"  session_id:      {_format_scalar(run.get('session_id'))}")

    summary = run.get("summary") or {}
    print("Summary")
    for label, key in (
        ("intent", "intent"),
        ("task", "task_kind"),
        ("stage", "task_stage"),
        ("response", "response_kind"),
        ("message", "response_message"),
        ("provider", "selected_provider"),
        ("broadcast_status", "broadcast_status"),
        ("broadcast_tx_hash", "broadcast_tx_hash"),
    ):
        print(f"  {label + ':':<17} {_format_scalar(summary.get(key))}")
    print(f"  {'route:':<17} {_format_scalar(summary.get('route'))}")
    print(f"  {'cards:':<17} {_format_scalar(summary.get('cards'))}")
    print(f"  {'event_count:':<17} {_format_scalar(summary.get('event_count'))}")

    tools = summary.get("tool_timeline") or []
    if tools:
        print("Tools")
        for item in tools:
            name = item.get("tool") or item.get("name") or "unknown"
            print(
                "  "
                f"{name}: status={_format_scalar(item.get('status'))} "
                f"elapsed_ms={_format_scalar(item.get('elapsed_ms'))} "
                f"error={_format_scalar(item.get('error_code'))}"
            )

    models = summary.get("models") or []
    if models:
        print("Models")
        for item in models:
            print(
                "  "
                f"{_format_scalar(item.get('role'))}: "
                f"{_format_scalar(item.get('model_id'))} "
                f"elapsed_ms={_format_scalar(item.get('elapsed_ms'))}"
            )

    errors = summary.get("errors") or []
    if errors:
        print("Errors")
        for error in errors:
            print(f"  {_format_scalar(error)}")

    if show_events:
        print("Events")
        for event in run.get("events") or []:
            body = {
                key: value
                for key, value in event.items()
                if key not in {"event_id", "event"}
            }
            print(
                f"  {_format_scalar(event.get('event_id'))} "
                f"{_format_scalar(event.get('event'))} "
                f"{json.dumps(body, ensure_ascii=False)}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect Wallet Agent runtime data through the read-only HTTP API."
    )
    lookup = parser.add_mutually_exclusive_group(required=True)
    lookup.add_argument("--run-id")
    lookup.add_argument("--conversation-id")
    lookup.add_argument("--session-id")
    parser.add_argument(
        "--base-url",
        default=os.getenv("WALLET_AGENT_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--token", default=os.getenv("WALLET_AGENT_TOKEN"))
    parser.add_argument(
        "--user-id",
        default=os.getenv("WALLET_AGENT_USER_ID", "browser-demo"),
        help="Fallback identity when the server has authentication disabled.",
    )
    parser.add_argument("--event-limit", type=int, default=200)
    parser.add_argument("--show-events", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    params = {
        "user_id": args.user_id,
        "event_limit": str(args.event_limit),
    }
    report: dict[str, Any] = {}
    try:
        with httpx.Client(
            base_url=args.base_url.rstrip("/"),
            headers=headers,
            timeout=args.timeout,
            trust_env=False,
        ) as client:
            if args.run_id:
                report["run"] = _get_json(
                    client,
                    f"/v1/agent/runs/{args.run_id}",
                    headers=headers,
                    params=params,
                )
            elif args.conversation_id:
                conversation = _get_json(
                    client,
                    f"/v1/agent/conversations/{args.conversation_id}",
                    headers=headers,
                    params={"user_id": args.user_id},
                )
                report["conversation"] = conversation
                run_id = (conversation.get("session") or {}).get("last_run_id")
                report["run"] = _run_from_lookup(
                    client,
                    headers=headers,
                    params=params,
                    run_id=run_id,
                )
            else:
                session = _get_json(
                    client,
                    f"/v1/swap/{args.session_id}",
                    headers=headers,
                    params={"user_id": args.user_id},
                )
                report["session"] = session
                report["run"] = _run_from_lookup(
                    client,
                    headers=headers,
                    params=params,
                    run_id=session.get("last_run_id"),
                )
    except (httpx.HTTPError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    conversation = report.get("conversation")
    if conversation:
        print("Conversation")
        print(f"  conversation_id: {_format_scalar(conversation.get('conversation_id'))}")
        print(f"  session_id:      {_format_scalar(conversation.get('session_id'))}")
        messages = conversation.get("messages") or []
        print(f"  messages:        {len(messages)}")
        for message in messages:
            role = _format_scalar(message.get("role"))
            content = _format_scalar(message.get("content"))
            print(f"  {role}: {content}")

    session = report.get("session")
    if session:
        print("Session")
        for key in (
            "session_id",
            "thread_id",
            "status",
            "stage",
            "last_run_id",
            "broadcast_status",
            "broadcast_tx_hash",
        ):
            print(f"  {key + ':':<18} {_format_scalar(session.get(key))}")

    run = report.get("run")
    if run:
        _print_run(run, show_events=args.show_events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
