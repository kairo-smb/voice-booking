"""Voice tool endpoints — catalog (services, staff)."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import (
    Envelope, GetServicesIn, GetStaffForServiceIn, ServiceOut, StaffOut,
)
from booking_engine.db.voice_tool_queries import (
    list_services, list_staff_for_service,
)

router = APIRouter(prefix="/voice/tools", tags=["voice-tools-catalog"])


@router.post("/get_services")
async def get_services(
    body: GetServicesIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[ServiceOut]]:
    rows = await list_services(shop_id=x_shop_id, filter_q=body.filter)
    out = [ServiceOut(service_id=r["id"], name=r["name"],
                      duration_min=r["duration_min"],
                      price_cents=r["price_cents"] if body.include_price else None)
           for r in rows]
    return Envelope[list[ServiceOut]](ok=True, data=out)


@router.post("/get_staff_for_service")
async def get_staff_for_service(
    body: GetStaffForServiceIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[StaffOut]]:
    rows = await list_staff_for_service(shop_id=x_shop_id, service_id=body.service_id)
    out = [StaffOut(staff_id=r["id"], name=r["name"]) for r in rows]
    return Envelope[list[StaffOut]](ok=True, data=out)