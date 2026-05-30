"""Control-plane endpoints for the voice agent."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from booking_engine.api.deps import require_control_plane_token
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
from booking_engine.db import voice_queries as vq


router = APIRouter(
    tags=["voice"],
    dependencies=[Depends(require_control_plane_token)],
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
