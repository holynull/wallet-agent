"""Persistence initialization hooks used by application startup and tests."""

from __future__ import annotations

from .checkpoints import CheckpointerHandle, create_checkpointer


def initialize_checkpointer(
    persistence_url: str,
    *,
    mysql_pool_min_size: int = 1,
    mysql_pool_max_size: int = 10,
    mysql_pool_recycle_seconds: int = 1800,
) -> CheckpointerHandle:
    """Initialize the configured checkpoint schema and return its owner handle."""
    return create_checkpointer(
        persistence_url,
        mysql_pool_min_size=mysql_pool_min_size,
        mysql_pool_max_size=mysql_pool_max_size,
        mysql_pool_recycle_seconds=mysql_pool_recycle_seconds,
    )
