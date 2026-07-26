"""Server-side authorization for reschedule/cancel — caller must own the booking.

We do NOT trust the agent's 'verification_passed'; the server checks that the
call's caller_number matches a phone on the appointment's customer, and that the
appointment belongs to the calling shop.
"""
from __future__ import annotations

from uuid import uuid4

from booking_engine.services.booking_authz import authorize_booking_change

SHOP = uuid4()
OTHER_SHOP = uuid4()


def _owner(shop_id=SHOP, phones=("+39 333 111 0000",)):
    return {"shop_id": shop_id, "customer_id": uuid4(), "phones": list(phones)}


def test_rejects_when_appointment_missing():
    ok, reason = authorize_booking_change(
        caller_number="+39 333 111 0000", call_shop_id=SHOP, owner=None)
    assert ok is False and reason == "appointment_not_found"


def test_rejects_appointment_from_another_shop():
    ok, reason = authorize_booking_change(
        caller_number="+39 333 111 0000", call_shop_id=SHOP,
        owner=_owner(shop_id=OTHER_SHOP))
    assert ok is False and reason == "wrong_shop"


def test_rejects_anonymous_caller():
    ok, reason = authorize_booking_change(
        caller_number="", call_shop_id=SHOP, owner=_owner())
    assert ok is False and reason == "anonymous_caller"


def test_rejects_phone_mismatch():
    ok, reason = authorize_booking_change(
        caller_number="+39 333 999 9999", call_shop_id=SHOP,
        owner=_owner(phones=("+39 333 111 0000",)))
    assert ok is False and reason == "phone_mismatch"


def test_allows_matching_phone_ignoring_formatting():
    ok, reason = authorize_booking_change(
        caller_number="+39 333 111 0000", call_shop_id=SHOP,
        owner=_owner(phones=("3933311100 00",)))
    assert ok is True and reason == "ok"


def test_allows_when_any_of_multiple_phones_matches():
    ok, reason = authorize_booking_change(
        caller_number="+39 333 111 0000", call_shop_id=SHOP,
        owner=_owner(phones=("+39 06 1234567", "+39 333 111 0000")))
    assert ok is True and reason == "ok"
