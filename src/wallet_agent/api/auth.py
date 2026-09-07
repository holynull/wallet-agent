"""Small, injectable authentication boundary for the HTTP API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class TokenVerifier(Protocol):
    def verify(self, token: str) -> str | None: ...


class StaticTokenVerifier:
    """Deterministic verifier useful for local deployments and unit tests."""

    def __init__(self, tokens: Mapping[str, str]) -> None:
        self.tokens = dict(tokens)

    def verify(self, token: str) -> str | None:
        return self.tokens.get(token)
