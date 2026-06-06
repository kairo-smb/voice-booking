"""Telephony provisioning + listing endpoints (Path 1 + Path 2 onboarding)."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from booking_engine.api.deps import require_control_plane_token
from booking_engine.clients.telnyx_numbers import (
    purchase_number,
    search_available_numbers,
)
from booking_engine.config import Settings, get_settings
from booking_engine.db import voice_telephony_queries as q

router = APIRouter(prefix="/voice/numbers", tags=["voice-telephony"])


class AvailableNumberOut(BaseModel):
    phone_number: str
    friendly_name: str
    locality: str
    region: str


class ProvisionIn(BaseModel):
    shop_id: UUID
    phone_number: str
    setup_path: str = Field(pattern=r"^(new|forward)$")
    salon_existing_number: str | None = None


class TelephonyOut(BaseModel):
    shop_id: UUID
    kairo_number: str
    kairo_number_sid: str
    setup_path: str
    salon_existing_number: str | None


@router.get("/search")
async def search(
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
    area_code: str | None = Query(default=None, max_length=4),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    results = search_available_numbers(
        area_code=area_code,
        country=settings.telnyx_default_country,
        limit=limit,
        api_key=settings.telnyx_api_key,
    )
    return {"data": [AvailableNumberOut(**r.__dict__).model_dump() for r in results]}


@router.post("/provision")
async def provision(
    body: ProvisionIn,
    settings: Annotated[Settings, Depends(get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    if body.setup_path == "forward" and not body.salon_existing_number:
        raise HTTPException(400, "salon_existing_number required for forward setup")

    voice_url = f"{settings.public_base_url}/voice/texml/incoming"
    purchased = purchase_number(
        phone_number=body.phone_number,
        voice_url=voice_url,
        api_key=settings.telnyx_api_key,
    )
    # Path 2 (new IT number) starts as pending_review; transitions to active/rejected
    # via the /voice/telnyx/number-status webhook once Telnyx completes the review.
    activation_status = "pending_review" if body.setup_path == "new" else "active"
    row = await q.upsert_telephony(
        shop_id=body.shop_id,
        provider="telnyx",
        kairo_number=purchased.phone_number,
        kairo_number_sid=purchased.sid,
        salon_existing_number=body.salon_existing_number,
        setup_path=body.setup_path,
        activation_status=activation_status,
    )
    return {"data": TelephonyOut(
        shop_id=row["shop_id"],
        kairo_number=row["kairo_number"],
        kairo_number_sid=row["kairo_number_sid"],
        setup_path=row["setup_path"],
        salon_existing_number=row["salon_existing_number"],
    ).model_dump()}


@router.get("/{shop_id}")
async def get_for_shop(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    row = await q.get_telephony(shop_id)
    if not row:
        return {"data": None}
    return {"data": TelephonyOut(
        shop_id=row["shop_id"],
        kairo_number=row["kairo_number"],
        kairo_number_sid=row["kairo_number_sid"],
        setup_path=row["setup_path"],
        salon_existing_number=row["salon_existing_number"],
    ).model_dump()}
