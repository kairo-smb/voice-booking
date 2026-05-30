"""Control-plane endpoints for the voice agent."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from booking_engine.api.deps import _get_settings
from booking_engine.api.voice_models import (
    VoiceConfigResponse,
    VoiceConfigUpdateRequest,
    CallSummary,
    CallDetail,
    TranscriptTurn,
    CallEvent,
    LinkCustomerRequest,
    VoiceAnalyticsResponse,
)
from booking_engine.config import Settings
from booking_engine.db import voice_queries as vq


async def _require_auth(
    request: Request,
    settings: Settings = Depends(_get_settings),
) -> bool:
    """Bearer-token guard for the control-plane. Mirrors deps.require_control_plane_token
    but avoids functools.wraps so FastAPI's dep-override mechanism works in tests."""
    if not settings.control_plane_secret:
        raise HTTPException(status_code=503, detail="control plane disabled")
    header = request.headers.get("authorization", "")
    expected = f"Bearer {settings.control_plane_secret}"
    if header != expected:
        raise HTTPException(status_code=401, detail="invalid token")
    return True


router = APIRouter(
    tags=["voice"],
    dependencies=[Depends(_require_auth)],
)


def _wrap(data) -> dict:
    return {"data": data}


@router.get("/shops/{shop_id}/voice/config")
async def get_voice_config(shop_id: UUID):
    cfg = await vq.get_voice_config(shop_id)
    if not cfg:
        return JSONResponse(
            status_code=404,
            content={"error": "shop_not_found", "message": f"Shop {shop_id} not found"},
        )
    return _wrap(VoiceConfigResponse(**cfg).model_dump(mode="json"))


@router.patch("/shops/{shop_id}/voice/config")
async def patch_voice_config(shop_id: UUID, body: VoiceConfigUpdateRequest):
    patch_dict = body.model_dump(exclude_unset=True)
    cfg = await vq.update_voice_config(shop_id, patch_dict)
    if not cfg:
        return JSONResponse(
            status_code=404,
            content={"error": "shop_not_found", "message": f"Shop {shop_id} not found"},
        )
    return _wrap(VoiceConfigResponse(**cfg).model_dump(mode="json"))
