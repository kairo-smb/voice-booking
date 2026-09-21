"""Routing a WhatsApp conversation — the pure core.

No clock, no database, no Meta: every case below is expressed as literal
history plus a literal verdict, which is the whole point of keeping this
decision pure.
"""
from datetime import datetime, timedelta, timezone

from booking_engine.services.messaging import wa_routing as r

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def msg(minutes_ago, intent=None, confidence=None):
    return {
        "received_at": NOW - timedelta(minutes=minutes_ago),
        "intent": intent,
        "confidence": confidence,
    }


# --- the session boundary ---------------------------------------------------

def test_session_starts_after_a_gap_over_24h():
    history = [msg(60 * 30), msg(60 * 2), msg(5)]   # 30h ago, then 2h, then 5m
    assert r.session_messages(history) == [msg(60 * 2), msg(5)]


def test_a_single_message_is_its_own_session():
    assert r.session_messages([msg(1)]) == [msg(1)]


def test_an_empty_history_has_an_empty_session():
    assert r.session_messages([]) == []


def test_a_gap_of_exactly_24h_does_not_start_a_new_session():
    history = [msg(60 * 24), msg(0)]
    assert r.session_messages(history) == history


def test_a_gap_of_a_minute_over_24h_does_start_a_new_session():
    history = [msg(60 * 24 + 1), msg(0)]
    assert r.session_messages(history) == [msg(0)]


def test_only_the_last_gap_counts_when_there_are_several():
    # Three sessions in the log; only the newest one is live.
    history = [msg(60 * 100), msg(60 * 70), msg(60 * 2), msg(1)]
    assert r.session_messages(history) == [msg(60 * 2), msg(1)]


# --- the session's verdict --------------------------------------------------

def test_routed_reads_the_latest_intent_in_the_session():
    history = [msg(60 * 30, "cancel", 0.9), msg(60, "booking", 0.9), msg(5)]
    assert r.routed_intent(history) == "booking"


def test_an_intent_from_a_previous_session_does_not_carry_over():
    history = [msg(60 * 30, "booking", 0.95), msg(5)]
    assert r.routed_intent(history) is None


def test_an_empty_history_has_no_routed_intent():
    assert r.routed_intent([]) is None


def test_an_unnamed_session_has_no_routed_intent():
    assert r.routed_intent([msg(10), msg(0)]) is None


# --- the decision -----------------------------------------------------------

def test_a_confident_whitelisted_verdict_routes():
    d = r.decide(history=[msg(0)], verdict={"intent": "booking", "confidence": 0.86})
    assert d == r.Decision("route", "booking")


def test_confidence_exactly_at_the_threshold_routes():
    d = r.decide(
        history=[msg(0)],
        verdict={"intent": "booking", "confidence": r.ROUTING_CONFIDENCE},
    )
    assert d == r.Decision("route", "booking")


def test_low_confidence_on_the_first_message_sends_the_menu():
    d = r.decide(history=[msg(0)], verdict={"intent": "booking", "confidence": 0.4})
    assert d == r.Decision("menu", None)


def test_low_confidence_again_after_the_menu_goes_to_a_human():
    history = [msg(10), msg(0)]        # two turns spent
    d = r.decide(history=history, verdict={"intent": "booking", "confidence": 0.4})
    assert d == r.Decision("human", None)


def test_an_intent_outside_the_whitelist_goes_straight_to_a_human():
    # Confident, and precisely because it is confident we know it is not ours.
    d = r.decide(history=[msg(0)], verdict={"intent": "complaint", "confidence": 0.99})
    assert d == r.Decision("human", "complaint")


def test_an_unconfident_intent_outside_the_whitelist_still_gets_the_menu():
    # Not confident is not confident, whatever it guessed at — the menu is
    # cheaper and more accurate than acting on a guess we do not believe.
    d = r.decide(history=[msg(0)], verdict={"intent": "complaint", "confidence": 0.3})
    assert d == r.Decision("menu", None)


def test_a_verdict_with_no_intent_at_all_gets_the_menu():
    d = r.decide(history=[msg(0)], verdict={"intent": None, "confidence": 0.99})
    assert d == r.Decision("menu", None)


def test_a_refused_classifier_never_guesses():
    # verdict=None is the empty-AI-basket / classifier-error case.
    assert r.decide(history=[msg(0)], verdict=None) == r.Decision("human", None)


def test_a_missing_confidence_is_treated_as_zero():
    d = r.decide(history=[msg(0)], verdict={"intent": "booking"})
    assert d == r.Decision("menu", None)


def test_a_non_numeric_confidence_is_treated_as_zero():
    d = r.decide(history=[msg(0)], verdict={"intent": "booking", "confidence": "high"})
    assert d == r.Decision("menu", None)


def test_a_boolean_confidence_is_treated_as_zero():
    # float(True) is 1.0 — a malformed payload must not route by accident.
    d = r.decide(history=[msg(0)], verdict={"intent": "booking", "confidence": True})
    assert d == r.Decision("menu", None)


def test_a_numeric_string_confidence_is_still_read_as_a_number():
    d = r.decide(history=[msg(0)], verdict={"intent": "booking", "confidence": "0.9"})
    assert d == r.Decision("route", "booking")


# --- buttons ----------------------------------------------------------------

def test_a_button_tap_routes_with_no_verdict_at_all():
    assert r.decide(history=[msg(0)], button_id="booking") == r.Decision("route", "booking")


def test_an_unknown_button_id_is_not_trusted():
    assert r.decide(history=[msg(0)], button_id="../admin") == r.Decision("human", None)


def test_a_button_tap_wins_over_a_verdict():
    d = r.decide(
        history=[msg(0)],
        verdict={"intent": "cancel", "confidence": 0.99},
        button_id="booking",
    )
    assert d == r.Decision("route", "booking")


def test_a_button_tap_late_in_a_session_still_routes():
    # The turn cap is about blind classifier guesses, not about a tap the
    # customer made on ids we defined ourselves.
    d = r.decide(history=[msg(20), msg(10), msg(0)], button_id="hours")
    assert d == r.Decision("route", "hours")


# --- the module is pure -----------------------------------------------------

def test_the_module_imports_no_clock_and_no_io():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(r))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)

    banned = ("booking_engine.db", "asyncpg", "httpx", "datetime.datetime")
    assert not [name for name in imported if name.startswith(banned)]
    assert "datetime.datetime" not in imported
