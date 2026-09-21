"""Errors raised by the signed OKX transport."""

from __future__ import annotations


class OkxClientError(Exception):
    """A sanitized transport or application-level OKX failure."""

    def __init__(
        self,
        code: str,
        message: str = "OKX request failed",
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.code = str(code)
        self.message = str(message)
        self.retryable = retryable
        self.status_code = status_code
        detail = f" ({status_code})" if status_code is not None else ""
        super().__init__(f"OKX request {self.code}{detail}: {self.message}")
