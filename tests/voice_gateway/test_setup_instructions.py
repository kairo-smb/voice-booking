"""Tests for the per-shop forwarding setup-instruction generator."""
from __future__ import annotations

from booking_engine.services.setup_instructions import build_instructions

DID = "+390212345678"


def _mobile(**kw):
    base = dict(
        kairo_number=DID,
        salon_existing_number="+39 333 1234567",
        answer_mode="overflow",
        overflow_ring_count=4,
    )
    base.update(kw)
    return build_instructions(**base)


def test_detects_mobile_from_italian_number():
    assert _mobile()["line_type"] == "mobile"


def test_detects_landline_from_italian_number():
    out = _mobile(salon_existing_number="06 1234567")
    assert out["line_type"] == "landline"


def test_mobile_overflow_emits_no_answer_busy_and_unreachable_codes():
    codes = _mobile(overflow_ring_count=4)["overflow"]["codes"]
    assert f"**61*{DID}*11*20#" in codes  # 4 rings x 5s = 20s no-answer timer
    assert f"**67*{DID}#" in codes  # busy
    assert f"**62*{DID}#" in codes  # unreachable / phone off


def test_overflow_seconds_clamped_to_30():
    codes = _mobile(overflow_ring_count=10)["overflow"]["codes"]  # 10x5=50 -> 30
    assert f"**61*{DID}*11*30#" in codes


def test_overflow_seconds_clamped_to_min_5():
    codes = _mobile(overflow_ring_count=1)["overflow"]["codes"]  # 1x5=5 -> 5
    assert f"**61*{DID}*11*5#" in codes


def test_mobile_full_emits_unconditional_code():
    codes = _mobile(answer_mode="always_on")["full"]["codes"]
    assert f"**21*{DID}#" in codes


def test_recommended_reflects_answer_mode():
    assert _mobile(answer_mode="always_on")["recommended"] == "always_on"
    assert _mobile(answer_mode="overflow")["recommended"] == "overflow"


def test_landline_overflow_is_best_effort_without_timer():
    out = _mobile(salon_existing_number="06 1234567")
    assert out["overflow"]["best_effort"] is True
    # landline codes omit the settable timer segment
    assert all("*11*" not in c for c in out["overflow"]["codes"])


def test_mobile_overflow_is_not_best_effort():
    assert _mobile()["overflow"]["best_effort"] is False
