"""Request authentication helpers shared by API handlers."""

from __future__ import annotations

from fastapi import HTTPException, Request

from .auth import TokenVerifier


async def authenticated_user(
    request: Request,
    *,
    verifier: TokenVerifier | None,
    required: bool,
    fallback_user_id: str | None = None,
) -> str:
    authorization = request.headers.get("authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
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
