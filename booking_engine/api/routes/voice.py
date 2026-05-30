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


@router.get("/shops/{shop_id}/voice/calls")
async def list_calls(
    shop_id: UUID,
    outcome: Annotated[list[str] | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    filters = {"outcome": outcome, "from": from_, "to": to, "q": q}
    result = await vq.list_calls(shop_id, filters=filters, cursor=cursor, limit=limit)
    items = [CallSummary(**r).model_dump(mode="json") for r in result["items"]]
    return {"data": items, "next_cursor": result["next_cursor"]}


@router.get("/shops/{shop_id}/voice/calls/{call_id}")
async def get_call_detail(shop_id: UUID, call_id: UUID):
    detail = await vq.get_call_detail(shop_id, call_id)
    if not detail:
        return JSONResponse(
            status_code=404,
            content={"error": "call_not_found", "message": f"Call {call_id} not found"},
        )
    payload = CallDetail(
        call=CallSummary(**detail["call"]),
        transcript=[TranscriptTurn(**t) for t in detail["transcript"]],
        events=[CallEvent(**e) for e in detail["events"]],
    )
    return _wrap(payload.model_dump(mode="json"))


@router.patch("/shops/{shop_id}/voice/calls/{call_id}/link-customer")
async def link_customer(shop_id: UUID, call_id: UUID, body: LinkCustomerRequest):
    updated = await vq.link_customer(shop_id, call_id, body.customer_id)
    if not updated:
        return JSONResponse(
            status_code=404,
            content={"error": "call_not_found", "message": f"Call {call_id} not found"},
        )
    return _wrap(CallSummary(**updated).model_dump(mode="json"))
