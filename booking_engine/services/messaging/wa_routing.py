"""Naming a WhatsApp request — a phase of the conversation, not a property of
each message.

Pure by design: no clock, no database, no Meta. The awkward parts here are the
session boundary and the turn cap, and both are exactly the kind of thing that
is impossible to reason about once it is tangled with IO. The same shape as
`number_release.decide_release` and `number_health.decide_health`: the policy
is a function of its arguments, and the IO lives in the caller.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, NamedTuple

# The set a handler may act on. Everything else — price, complaint, promo_reply,
# opt_out, other — is a human's. Routing is an explicit allowlist, never the
# absence of a red flag.
WHITELIST = ("booking", "reschedule", "cancel", "hours")

ROUTING_CONFIDENCE = 0.7

# One call on the opening message; if that misses, the button menu goes out and
# the model gets one more try. Two rather than three because the menu sits
# between the attempts — a second blind call on a conversation the model already
# failed once is what the menu exists to replace.
MAX_ROUTING_TURNS = 2

# The same boundary as Meta's service window, reused rather than reinvented: a
# customer writing again after a longer silence has a new request.
SESSION_GAP = timedelta(hours=24)


class Decision(NamedTuple):
    action: str            # 'route' | 'menu' | 'human'
    intent: str | None


def session_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tail of `history` since the last gap longer than SESSION_GAP.

    `history` is ascending by received_at — oldest first. A gap of exactly
    SESSION_GAP is still the same session: the boundary is strictly greater,
    matching Meta's own window, which is open *for* 24 hours.
    """
    if not history:
        return []
    start = 0
    for i in range(1, len(history)):
        if history[i]["received_at"] - history[i - 1]["received_at"] > SESSION_GAP:
            start = i
    return history[start:]


def routed_intent(history: list[dict[str, Any]]) -> str | None:
    """The session's verdict, derived rather than stored — the latest non-NULL
    intent inside the current session. A verdict from a previous session is
    deliberately invisible."""
    for m in reversed(session_messages(history)):
        if m.get("intent"):
            return str(m["intent"])
    return None


def _confidence(value: Any) -> float:
    """Whatever the classifier put in the field, as a number — or 0.

    The verdict is parsed out of a model's JSON, so `confidence` can be
    missing, null, a word ("high"), or a bool. None of those is a reason to
    crash a live conversation, and none of them is evidence of confidence
    either, so every unreadable value is no confidence at all. `bool` is
    excluded explicitly because it *is* a number in Python — `float(True)` is
    1.0, which would route a malformed payload rather than refuse it.
    """
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def decide(
    *,
    history: list[dict[str, Any]],
    verdict: dict[str, Any] | None = None,
    button_id: str | None = None,
) -> Decision:
    """What to do with the message that just arrived.

    `history` includes that message. `verdict` is the classifier's answer, or
    None when it was not consulted (a button tap) or refused (empty basket).
    """
    # A tap is an id we defined, so it needs no model — but it is still input
    # from outside, so it is checked against the whitelist rather than trusted.
    if button_id is not None:
        return Decision("route", button_id) if button_id in WHITELIST \
            else Decision("human", None)

    if verdict is None:
        return Decision("human", None)

    intent = str(verdict.get("intent") or "")
    confidence = _confidence(verdict.get("confidence"))

    if intent in WHITELIST and confidence >= ROUTING_CONFIDENCE:
        return Decision("route", intent)

    # Confident about something that is not ours: a human, and we know why.
    if intent and intent not in WHITELIST and confidence >= ROUTING_CONFIDENCE:
        return Decision("human", intent)

    # Not confident. One menu, then a human — never a third guess.
    if len(session_messages(history)) < MAX_ROUTING_TURNS:
        return Decision("menu", None)
    return Decision("human", None)
