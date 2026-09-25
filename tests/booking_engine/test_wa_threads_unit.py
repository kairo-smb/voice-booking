"""The 24h service window, and what puts a thread in front of the owner.

Pure: no clock, no database. The window helpers take `now` as an argument for
the same reason `decide_release` does — an off-by-one here is a send that fails
at Meta with 131047, which reaches the owner as an opaque provider error.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from booking_engine.db import whatsapp_thread_queries as th
from booking_engine.services.messaging import wa_routing

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def T(hour, minute=0):
    return datetime(2026, 9, 21, hour, minute, tzinfo=timezone.utc)


# --- the window -------------------------------------------------------------

def test_the_window_is_measured_from_the_last_customer_message():
    assert th.window_expires_at(last_inbound=T(12, 0)) == T(12, 0) + timedelta(hours=24)


def test_a_thread_with_no_inbound_has_no_window():
    assert th.window_expires_at(last_inbound=None) is None


def test_the_window_is_open_strictly_inside_24h():
    assert th.window_open(last_inbound=NOW - timedelta(hours=23, minutes=59), now=NOW)
    assert not th.window_open(last_inbound=NOW - timedelta(hours=24, seconds=1), now=NOW)


def test_at_exactly_24h_the_window_is_closed():
    # Pinned deliberately: the boundary is `now < expires`, so the instant the
    # 24th hour is reached there is nothing left to send into. Erring closed
    # costs a template; erring open costs a 131047 nobody can act on.
    assert not th.window_open(last_inbound=NOW - timedelta(hours=24), now=NOW)


def test_a_thread_with_no_inbound_is_never_open_and_does_not_raise():
    assert th.window_open(last_inbound=None, now=NOW) is False


def test_our_own_reply_does_not_extend_the_window():
    # Stated as a test because it is the one rule the whole feature rests on:
    # the expiry is a function of the customer's last message and nothing else.
    assert th.window_expires_at(last_inbound=T(9, 0)) == T(9, 0) + timedelta(hours=24)
    assert th.SERVICE_WINDOW == timedelta(hours=24)


# --- what needs a human -----------------------------------------------------

def test_needs_attention_when_the_session_is_unrouted():
    assert th.needs_attention({"intent": None, "escalated": False})


def test_needs_attention_when_the_intent_is_outside_the_whitelist():
    assert th.needs_attention({"intent": "complaint", "escalated": False})


def test_a_routed_booking_does_not_need_attention():
    assert not th.needs_attention({"intent": "booking", "escalated": False})


def test_an_escalated_thread_needs_attention_even_when_routed():
    # Escalation overrides routing: the agent had the conversation, named it,
    # and then handed it back. A whitelisted intent must not hide that.
    assert th.needs_attention({"intent": "booking", "escalated": True})


def test_a_thread_row_without_an_escalated_key_is_judged_on_its_intent():
    assert not th.needs_attention({"intent": "booking"})
    assert th.needs_attention({"intent": None})


def test_an_empty_thread_row_needs_attention():
    assert th.needs_attention({})


# --- one whitelist, not two -------------------------------------------------

def test_the_whitelist_is_wa_routings_and_not_a_second_copy():
    # A literal copied into this module would drift the day a new intent is
    # added, and the drift is silent: threads the router considers handled
    # would keep asking the owner to handle them.
    assert th.WHITELIST is wa_routing.WHITELIST
    for intent in wa_routing.WHITELIST:
        assert not th.needs_attention({"intent": intent, "escalated": False})


# --- the verdict written back -----------------------------------------------

@pytest.fixture
def recorded(monkeypatch):
    """The arguments set_verdict would send to Postgres, without a Postgres."""
    calls = []

    async def fake(sql, *args):
        calls.append(args)

    monkeypatch.setattr(th, "execute_void", fake)
    return calls


@pytest.mark.asyncio
async def test_a_menu_decision_leaves_the_intent_null(recorded):
    # Load-bearing: an intent written here would make the session look routed,
    # the worker would skip the classifier on the next message, and the menu
    # just sent would be answered by nobody.
    mid = uuid4()
    await th.set_verdict(
        mid, {"intent": "booking", "confidence": 0.3},
        wa_routing.Decision("menu", None),
    )
    assert recorded[0][0] == mid
    assert recorded[0][1] is None


@pytest.mark.asyncio
async def test_a_routed_decision_writes_the_intent_it_routed_on(recorded):
    await th.set_verdict(
        uuid4(), {"intent": "booking", "confidence": 0.9, "summary": "sabato"},
        wa_routing.Decision("route", "booking"),
    )
    assert recorded[0][1] == "booking"
    assert recorded[0][2] == 0.9
    assert recorded[0][3] == "sabato"


@pytest.mark.asyncio
async def test_a_human_decision_keeps_the_reason_it_named(recorded):
    await th.set_verdict(
        uuid4(), {"intent": "complaint", "confidence": 0.95},
        wa_routing.Decision("human", "complaint"),
    )
    assert recorded[0][1] == "complaint"


@pytest.mark.asyncio
async def test_an_unreadable_confidence_is_stored_as_null_not_raised(recorded):
    # `confidence` is numeric in Postgres and comes out of a model's JSON.
    # Losing the whole update over an unparseable number would lose the intent.
    await th.set_verdict(
        uuid4(), {"intent": "booking", "confidence": "alta"},
        wa_routing.Decision("route", "booking"),
    )
    assert recorded[0][1] == "booking"
    assert recorded[0][2] is None
