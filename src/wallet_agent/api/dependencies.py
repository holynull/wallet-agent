"""Request authentication helpers shared by API handlers."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from .auth import TokenVerifier


def bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    return authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""


def is_admin_token(request: Request, admin_token: str | None) -> bool:
    """Compare the operator token without exposing token values in errors."""
    token = bearer_token(request)
    if not token or not admin_token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), admin_token.encode("utf-8"))


async def authenticated_user(
    request: Request,
    *,
    verifier: TokenVerifier | None,
    required: bool,
    fallback_user_id: str | None = None,
) -> str:
    token = bearer_token(request)
    if verifier is not None:
        if not token:
            raise HTTPException(status_code=401, detail="Bearer token is required")
        user_id = verifier.verify(token)
        if hasattr(user_id, "__await__"):
            user_id = await user_id
        if not user_id:
            raise HTTPException(status_code=401, detail="Bearer token is invalid")
        return str(user_id)
    if required:
        raise HTTPException(status_code=401, detail="Bearer token authentication is required")
    return fallback_user_id or "anonymous"
