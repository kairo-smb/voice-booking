"""Shared FastAPI dependencies for the Booking Engine."""
from __future__ import annotations

import functools
from fastapi import Depends, HTTPException, Request

from booking_engine.config import Settings


def _get_settings() -> Settings:
    return Settings()


async def _require_control_plane_token_impl(
    request: Request,
    settings: Settings,
) -> bool:
    if not settings.control_plane_secret:
        raise HTTPException(status_code=503, detail="control plane disabled")
    header = request.headers.get("authorization", "")
    expected = f"Bearer {settings.control_plane_secret}"
    if header != expected:
        raise HTTPException(status_code=401, detail="invalid token")
    return True


@functools.wraps(_require_control_plane_token_impl)
async def require_control_plane_token(
    request: Request,
    settings: Settings = Depends(_get_settings),
) -> bool:
    return await _require_control_plane_token_impl(request, settings)
