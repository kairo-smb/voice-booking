"""Voice tool endpoints — availability + booking write/modify/cancel."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from booking_engine.api.deps import require_tool_token
from booking_engine.api.voice_tool_models import (
    AvailabilityChain, BookingOut, CancelBookingIn, CheckAvailabilityIn,
    CreateBookingIn, Envelope, ModifyBookingIn,
)
from booking_engine.config import Settings, get_settings
from booking_engine.db.voice_tool_queries import (
    attach_booking_to_call, find_availability, insert_booking_locked,
    service_belongs_to_shop,
)
from booking_engine.services.booking_constraints import (
    slot_in_past, within_lead_time,
)

router = APIRouter(prefix="/voice/tools", tags=["voice-tools-booking"])


@router.post("/check_availability")
async def check_availability(
    body: CheckAvailabilityIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
) -> Envelope[list[AvailabilityChain]]:
    services = [{"service_id": s.service_id, "staff_id": s.staff_id} for s in body.services]
    rows = await find_availability(
        shop_id=x_shop_id, services=services,
        preferred_when=body.preferred_when, max_results=body.max_results,
    )
    out = [AvailabilityChain(**r) for r in rows]
    return Envelope[list[AvailabilityChain]](ok=True, data=out)


@router.post("/create_booking")
async def create_booking(
    body: CreateBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    x_shop_id: Annotated[UUID, Header(alias="X-Shop-Id")],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[BookingOut]:
    for leg in body.legs:
        if not await service_belongs_to_shop(shop_id=x_shop_id, service_id=leg.service_id):
            return Envelope[BookingOut](ok=False, error="unknown_service")
    now = datetime.now(timezone.utc)
    if any(slot_in_past(leg.slot_start, now) for leg in body.legs):
        return Envelope[BookingOut](ok=False, error="slot_in_past")
    try:
        row = await insert_booking_locked(
            shop_id=x_shop_id, customer_id=body.customer_id,
            legs=[{"service_id": l.service_id, "staff_id": l.staff_id,
                   "slot_start": l.slot_start} for l in body.legs],
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
            legs=row["legs"],
        ),
    )


from booking_engine.db.voice_tool_queries import (
    cancel_appointment, get_appointment_owner, get_next_booking_for_customer,
    log_auth_event, modify_appointment,
)
from booking_engine.db.voice_calls_queries import get_call
from booking_engine.services.booking_authz import authorize_booking_change


async def _authorize_change(*, call_id: UUID, appointment_id: UUID, action: str):
    """Server-side ownership check. Returns (owner, reason) — reason 'ok' allows.

    Ignores any agent-supplied verification_passed; the caller's phone (from the
    call row) must own the appointment, and the appointment must be this shop's.
    """
    call = await get_call(call_id)
    if not call:
        await log_auth_event(call_id=call_id, customer_id=None,
                             verification_question=action,
                             caller_answer_excerpt="call_not_found", passed=False)
        return None, "call_not_found"
    owner = await get_appointment_owner(appointment_id=appointment_id)
    ok, reason = authorize_booking_change(
        caller_number=call.get("caller_number"),
        call_shop_id=call["shop_id"], owner=owner,
    )
    await log_auth_event(
        call_id=call_id,
        customer_id=owner.get("customer_id") if owner else None,
        verification_question=action, caller_answer_excerpt=reason, passed=ok,
    )
    return (owner, reason) if ok else (None, reason)


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
    settings: Annotated[Settings, Depends(get_settings)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    owner, reason = await _authorize_change(
        call_id=x_call_id, appointment_id=body.appointment_id,
        action="modify_booking",
    )
    if reason != "ok":
        return Envelope[dict](ok=False, error=reason)
    now = datetime.now(timezone.utc)
    if within_lead_time(owner["start_at"], now,
                        lead_hours=settings.voice_cancellation_lead_time_hours):
        return Envelope[dict](ok=False, error="reschedule_too_close")
    if body.new_slot_start and slot_in_past(body.new_slot_start, now):
        return Envelope[dict](ok=False, error="slot_in_past")
    if body.new_service_id and not await service_belongs_to_shop(
        shop_id=owner["shop_id"], service_id=body.new_service_id,
    ):
        return Envelope[dict](ok=False, error="unknown_service")
    ok = await modify_appointment(
        shop_id=owner["shop_id"],
        appointment_id=body.appointment_id,
        new_slot_start=body.new_slot_start,
        new_service_id=body.new_service_id,
    )
    return Envelope[dict](ok=ok, data={"updated": ok})


@router.post("/cancel_booking")
async def cancel_booking(
    body: CancelBookingIn,
    _auth: Annotated[bool, Depends(require_tool_token)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_call_id: Annotated[UUID, Header(alias="X-Call-Id")],
) -> Envelope[dict]:
    owner, reason = await _authorize_change(
        call_id=x_call_id, appointment_id=body.appointment_id,
        action="cancel_booking",
    )
    if reason != "ok":
        return Envelope[dict](ok=False, error=reason)
    if within_lead_time(owner["start_at"], datetime.now(timezone.utc),
                        lead_hours=settings.voice_cancellation_lead_time_hours):
        return Envelope[dict](ok=False, error="cancel_too_close")
    ok = await cancel_appointment(
        shop_id=owner["shop_id"], appointment_id=body.appointment_id,
    )
    return Envelope[dict](ok=ok, data={"cancelled": ok})