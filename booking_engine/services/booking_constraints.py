"""Pure booking-policy checks (no DB), shared by the create/modify/cancel tools."""
from __future__ import annotations

from datetime import datetime, timedelta


def slot_in_past(slot: datetime, now: datetime) -> bool:
    return slot < now


def within_lead_time(start_at: datetime, now: datetime, *, lead_hours: int) -> bool:
    """True when the appointment is too close (or past) to self-serve change."""
    return start_at - now < timedelta(hours=lead_hours)
