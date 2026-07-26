"""Pure booking-policy checks (no DB), shared by the create/modify/cancel tools."""
from __future__ import annotations

from datetime import datetime, timedelta


def slot_in_past(slot: datetime, now: datetime) -> bool:
    return slot < now


def within_lead_time(start_at: datetime, now: datetime, *, lead_hours: int) -> bool:
    """True when the appointment is too close (or past) to self-serve change."""
    return start_at - now < timedelta(hours=lead_hours)


MAX_GAP_MINUTES = 20


def gap_within_limit(prev_end: datetime, next_start: datetime) -> bool:
    """True when next_start is at or after prev_end, and no more than
    MAX_GAP_MINUTES later — the max idle time allowed between two
    consecutive services in a multi-service booking."""
    return prev_end <= next_start <= prev_end + timedelta(minutes=MAX_GAP_MINUTES)
