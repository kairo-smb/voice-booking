"""Shared FastAPI dependencies for the Booking Engine."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from booking_engine.config import Settings


def _get_settings() -> Settings:
    return Settings()


async def require_tool_token(
    request: Request,
    settings: Annotated[Settings, Depends(_get_settings)],
) -> bool:
    """Bearer-auth dependency for tool + event webhooks (OpenAI → us)."""
    expected = settings.openai_tool_secret
    if not expected:
        raise HTTPException(500, "Tool secret not configured")
    header = request.headers.get("Authorization", "")
    if header != f"Bearer {expected}":
        raise HTTPException(401, "Invalid tool token")
    return True


async def require_control_plane_token(
    request: Request,
    settings: Annotated[Settings, Depends(_get_settings)],
) -> bool:
    if not settings.control_plane_secret:
        raise HTTPException(status_code=503, detail="control plane disabled")
    header = request.headers.get("authorization", "")
    expected = f"Bearer {settings.control_plane_secret}"
    if header != expected:
        raise HTTPException(status_code=401, detail="invalid token")
    return True
