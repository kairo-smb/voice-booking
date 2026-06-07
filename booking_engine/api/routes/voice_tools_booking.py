"""Voice tool endpoints — availability + booking write/modify/cancel."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import (
    AvailabilitySlot, BookingOut, CancelBookingIn, CreateBookingIn, Envelope,
    ModifyBookingIn,
)
from booking_engine.db.voice_tool_queries import (
    attach_booking_to_call, find_availability, insert_booking_locked,
)

router = APIRouter(prefix="/voice/tools", tags=["voice-tools-booking"])


class CheckAvailabilityIn(BaseModel):
    service_id: UUID
    preferred_when: datetime | None = None
    staff_id: UUID | None = None
    max_results: int = 5


@router.post("/check_availability")
async def check_availability(
    body: CheckAvailabilityIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[AvailabilitySlot]]:
    rows = await find_availability(
        shop_id=x_shop_id, service_id=body.service_id,
        preferred_when=body.preferred_when, staff_id=body.staff_id,
        max_results=body.max_results,
    )
    out = [AvailabilitySlot(**r) for r in rows]
    return Envelope[list[AvailabilitySlot]](ok=True, data=out)


@router.post("/create_booking")
async def create_booking(
    body: CreateBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[BookingOut]:
    try:
        row = await insert_booking_locked(
            shop_id=x_shop_id, customer_id=body.customer_id,
            service_id=body.service_id, slot_start=body.slot_start,
            staff_id=body.staff_id,
        )
    except RuntimeError as e:
        return Envelope[BookingOut](ok=False, error=str(e))
    await attach_booking_to_call(call_id=x_call_id, appointment_id=row["id"])
    return Envelope[BookingOut](
        ok=True,
        data=BookingOut(
            appointment_id=row["id"],
            confirmation_status=row["confirmation_status"],
            slot_start=row["slot_start"], slot_end=row["slot_end"],
            staff_id=row["staff_id"],
        ),
    )


from booking_engine.db.voice_tool_queries import (
    cancel_appointment, get_next_booking_for_customer, log_auth_event,
    modify_appointment,
)


class GetBookingIn(BaseModel):
    customer_id: UUID
    fuzzy_when: str | None = None


@router.post("/get_booking")
async def get_booking(
    body: GetBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[dict | None]:
    row = await get_next_booking_for_customer(
        shop_id=x_shop_id, customer_id=body.customer_id,
    )
    return Envelope[dict | None](ok=True, data=row)


@router.post("/modify_booking")
async def modify_booking(
    body: ModifyBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    if not body.verification_passed:
        await log_auth_event(
            call_id=x_call_id, customer_id=None,
            verification_question="modify_booking",
            caller_answer_excerpt="", passed=False,
        )
        return Envelope[dict](ok=False, error="unauthorized")
    ok = await modify_appointment(
        appointment_id=body.appointment_id,
        new_slot_start=body.new_slot_start,
        new_service_id=body.new_service_id,
    )
    await log_auth_event(
        call_id=x_call_id, customer_id=None,
        verification_question="modify_booking",
        caller_answer_excerpt="", passed=True,
    )
    return Envelope[dict](ok=ok, data={"updated": ok})


@router.post("/cancel_booking")
async def cancel_booking(
    body: CancelBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    if not body.verification_passed:
        await log_auth_event(
            call_id=x_call_id, customer_id=None,
            verification_question="cancel_booking",
            caller_answer_excerpt="", passed=False,
        )
        return Envelope[dict](ok=False, error="unauthorized")
    ok = await cancel_appointment(appointment_id=body.appointment_id)
    await log_auth_event(
        call_id=x_call_id, customer_id=None,
        verification_question="cancel_booking",
        caller_answer_excerpt="", passed=True,
    )
    return Envelope[dict](ok=ok, data={"cancelled": ok})