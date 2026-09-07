"""Persistence initialization hooks used by application startup and tests."""

from __future__ import annotations

from .checkpoints import CheckpointerHandle, create_checkpointer


def initialize_checkpointer(persistence_url: str) -> CheckpointerHandle:
    """Initialize the configured checkpoint schema and return its owner handle."""
    return create_checkpointer(persistence_url)
