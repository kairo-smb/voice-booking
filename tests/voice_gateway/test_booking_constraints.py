"""Pure booking-policy checks: no past slots, cancellation/reschedule lead-time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from booking_engine.services.booking_constraints import (
    slot_in_past, within_lead_time,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def test_slot_in_past_true_for_earlier_time():
    assert slot_in_past(NOW - timedelta(hours=1), NOW) is True


def test_slot_in_past_false_for_future_time():
    assert slot_in_past(NOW + timedelta(hours=1), NOW) is False


def test_within_lead_time_true_when_appointment_is_close():
    assert within_lead_time(NOW + timedelta(hours=1), NOW, lead_hours=2) is True


def test_within_lead_time_false_when_appointment_is_far():
    assert within_lead_time(NOW + timedelta(hours=3), NOW, lead_hours=2) is False


def test_within_lead_time_true_for_past_appointment():
    # already past its start → definitely too close to self-serve
    assert within_lead_time(NOW - timedelta(hours=1), NOW, lead_hours=2) is True
