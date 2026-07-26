"""Control-plane endpoints for the voice agent."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from booking_engine.api.deps import require_control_plane_token
from booking_engine.api.voice_models import (
    CallSummary,
    CallDetail,
    TranscriptTurn,
    CallEvent,
    LinkCustomerRequest,
    VoiceAnalyticsResponse,
)
from booking_engine.db import voice_queries as vq
from booking_engine.db.voice_tool_queries import list_services
from booking_engine.services.service_catalog_match import enrich_brief, parse_brief


router = APIRouter(
    tags=["voice"],
    dependencies=[Depends(require_control_plane_token)],
)

def _wrap(data) -> dict:
    return {"data": data}


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
    brief = parse_brief(detail.get("service_brief"))
    if brief.get("services_requested"):
        catalog = await list_services(shop_id=shop_id, filter_q=None)
        brief = enrich_brief(brief, catalog)
    payload = CallDetail(
        call=CallSummary(**detail["call"]),
        transcript=[TranscriptTurn(**t) for t in detail["transcript"]],
        events=[CallEvent(**e) for e in detail["events"]],
        service_brief=brief,
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


@router.get("/shops/{shop_id}/voice/analytics")
async def get_analytics(
    shop_id: UUID,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
):
    result = await vq.get_analytics(shop_id, from_dt=from_, to_dt=to)
    return _wrap(VoiceAnalyticsResponse(**result).model_dump(mode="json"))
