"""Validation tests for voice control-plane Pydantic models."""
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from pydantic import ValidationError

from booking_engine.api.voice_models import (
    CallSummary,
    CallDetail,
    TranscriptTurn,
    CallEvent,
    LinkCustomerRequest,
    VoiceAnalyticsResponse,
)

def test_call_summary_round_trip():
    payload = {
        "id": str(uuid4()),
        "caller_number": "+39000",
        "customer_id": None,
        "customer_match": "unmatched",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": None,
        "duration_seconds": None,
        "outcome": None,
        "summary": None,
        "appointment_id": None,
    }
    m = CallSummary(**payload)
    assert m.customer_match == "unmatched"


def test_call_summary_rejects_unknown_outcome():
    with pytest.raises(ValidationError):
        CallSummary(
            id=str(uuid4()), caller_number="+39", customer_match="existing",
            started_at=datetime.now(timezone.utc), outcome="nope",
        )


def test_link_customer_request_requires_uuid():
    with pytest.raises(ValidationError):
        LinkCustomerRequest(customer_id="not-a-uuid")


def test_call_detail_aggregates():
    cs_id = uuid4()
    summary = CallSummary(
        id=cs_id, caller_number="+39", customer_match="existing",
        started_at=datetime.now(timezone.utc),
    )
    d = CallDetail(call=summary, transcript=[], events=[])
    assert d.call.id == cs_id


def test_analytics_response_shape():
    a = VoiceAnalyticsResponse(
        volume={"total": 0, "by_day": [], "avg_duration_sec": 0, "failure_rate": 0.0},
        outcomes={"booked": 0, "rescheduled": 0, "cancelled": 0, "info": 0,
                  "abandoned": 0, "escalated": 0, "failed": 0, "conversion_rate": 0.0},
        demand={"top_services": [], "top_staff": [],
                "by_hour": [], "by_dow": [], "after_hours_pct": 0.0},
    )
    assert a.volume["total"] == 0
