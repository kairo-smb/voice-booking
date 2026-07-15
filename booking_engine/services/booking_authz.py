"""Authorize a reschedule/cancel: the caller must own the booking.

Server-side trust boundary — do NOT rely on the agent asserting the caller was
verified. The call row (caller_number, shop_id) is created by us; the appointment
owner's phones come from the DB. A caller may only change a booking that (a)
belongs to their shop and (b) is registered to their calling number.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from booking_engine.services.phone_normalize import digits_only


def authorize_booking_change(
    *,
    caller_number: str | None,
    call_shop_id: UUID | str,
    owner: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Return (allowed, reason). reason is 'ok' when allowed."""
    if owner is None:
        return False, "appointment_not_found"
    if str(owner["shop_id"]) != str(call_shop_id):
        return False, "wrong_shop"
    caller_digits = digits_only(caller_number)
    if not caller_digits:
        return False, "anonymous_caller"
    owner_digits = {digits_only(p) for p in owner.get("phones", [])}
    if caller_digits not in owner_digits:
        return False, "phone_mismatch"
    return True, "ok"
