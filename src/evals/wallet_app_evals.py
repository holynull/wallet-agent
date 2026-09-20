"""Offline Wallet App lifecycle evaluation suite and JSON command-line runner.

The coordinator deliberately uses the public-contract scenario harness only.  It
does not load application settings, read credentials, sign transactions, or make
network requests outside the deterministic ASGI test app.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .wallet_app_scenarios import (
    SCENARIOS,
    ScenarioDefinition,
    build_scenario_runtime,
    drive_scenario,
    run_scenario,
)
from .wallet_app_simulator import LifecycleReport, sanitize_evidence


class UnknownScenarioError(ValueError):
    """Raised when a requested scenario ID is not registered."""

    def __init__(self, scenario_id: str) -> None:
        self.scenario_id = scenario_id
        super().__init__(f"unknown scenario: {scenario_id}")


async def run_definition(
    definition: ScenarioDefinition, *, max_attempts: int = 3
) -> LifecycleReport:
    """Run one supplied definition, including deliberate expectation mutations.

    This small internal seam is useful for evaluator regression tests: callers can
    replace an expectation on a copied definition and verify that observable
    mismatches produce a failed report and a non-zero CLI exit code.
    """

    runtime = await build_scenario_runtime(definition)
    try:
        return await drive_scenario(runtime, max_attempts=max_attempts)
    finally:
        await runtime.http.aclose()


async def _run_registered(scenario_id: str) -> LifecycleReport:
    # Keep the normal path on run_scenario so its unknown-ID and lifecycle cleanup
    # semantics remain the single source of truth.
    return await run_scenario(scenario_id)


def aggregate_reports(reports: Sequence[LifecycleReport]) -> dict[str, Any]:
    """Aggregate serialized lifecycle reports without expected-count constants."""

    scenarios = [report.model_dump() for report in reports]
    summary = {
        "total": len(scenarios),
        "passed": sum(item["status"] == "passed" for item in scenarios),
        "failed": sum(item["status"] == "failed" for item in scenarios),
        "blocked": sum(item["status"] == "blocked" for item in scenarios),
    }
    dimensions: dict[str, dict[str, int]] = {}
    for dimension in (
        "wallet_api_contract",
        "broadcast_and_confirmation",
        "safety",
    ):
        selected = [
            item for item in scenarios if dimension in item.get("dimensions", ())
        ]
        dimensions[dimension] = {
            "total": len(selected),
            "passed": sum(item["status"] == "passed" for item in selected),
        }

    return sanitize_evidence(
        {
            "schema_version": 1,
            "mode": "offline-wallet-app",
            "summary": summary,
            "dimensions": dimensions,
            "scenarios": scenarios,
        }
    )


async def run_wallet_app_evals(scenario_id: str | None = None) -> dict[str, Any]:
    """Run all scenarios (or one ID) and return a stable serializable report."""

    if scenario_id is not None and scenario_id not in SCENARIOS:
        raise UnknownScenarioError(scenario_id)
    ids = [scenario_id] if scenario_id is not None else list(SCENARIOS)
    reports = [await _run_registered(item) for item in ids]
    return aggregate_reports(reports)


def exit_code(report: Mapping[str, Any]) -> int:
    """Return 0 for a fully passing report, otherwise 1 for scenario failures."""

    summary = report["summary"]
    return 0 if summary["failed"] == 0 and summary["blocked"] == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline suite and emit exactly one JSON document on stdout."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse has already explained the usage error on stderr.  Keep the
        # machine-facing stdout contract to one JSON document as well.
        if int(exc.code or 0) == 0:
            raise
        print(
            json.dumps(
                {"code": "USAGE_ERROR", "message": "invalid command-line arguments"},
                ensure_ascii=False,
            )
        )
        return 2

    try:
        report = asyncio.run(run_wallet_app_evals(args.scenario))
    except UnknownScenarioError as exc:
        print(
            json.dumps(
                {"code": "UNKNOWN_SCENARIO", "message": str(exc)},
                ensure_ascii=False,
            )
        )
        return 2
    except Exception as exc:  # evaluator setup/configuration errors
        print(
            json.dumps(
                {
                    "code": "EVALUATOR_ERROR",
                    "message": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code(report)


if __name__ == "__main__":  # pragma: no cover - exercised by module invocation
    raise SystemExit(main())
