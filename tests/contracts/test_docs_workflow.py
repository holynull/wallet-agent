from __future__ import annotations

from pathlib import Path


DOC_PATH = Path(__file__).parents[2] / "docs" / "local-demo-debugging.md"


def test_debugging_guide_names_contract_fixtures_errors_and_verification_commands():
    content = DOC_PATH.read_text(encoding="utf-8")

    required_fragments = (
        "tests/contracts/fixtures/okx/",
        "tests/contracts/fixtures/providers/",
        "OKX_MALFORMED_RESPONSE",
        "ASSET_NOT_FOUND",
        "PROVIDER_QUOTE_FAILED",
        "pytest tests/contracts tests/evals -q",
        "pytest -m integration -q",
        "python -m evals.wallet_app_evals",
        "ruff check .",
        "git diff --check",
        "run_id",
        "SSE update",
        "conversation_id",
        "safe_shape",
        "脱敏",
    )

    missing = [fragment for fragment in required_fragments if fragment not in content]
    assert not missing, f"debugging guide is missing required workflow terms: {missing}"


def test_debugging_guide_does_not_contain_fixture_secrets_or_real_credentials():
    content = DOC_PATH.read_text(encoding="utf-8")

    forbidden_fragments = (
        "BEGIN PRIVATE KEY",
        "-----BEGIN",
        "api_secret=",
        "secret_key=",
        "Authorization: Bearer eyJ",
    )
    for fragment in forbidden_fragments:
        assert fragment not in content
