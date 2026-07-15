"""Generate the exact call-forwarding codes a salon types to route calls to us.

Two product modes (see spec 2026-07-15-call-forwarding-overflow-design):
  - Full (always_on): forward *every* call to the DID; the AI is the receptionist.
  - Overflow: forward only unanswered/busy calls; the AI catches misses.

Mobile SIMs support these as self-serve GSM supplementary-service codes.
Italian copper landlines usually support the immediate/no-reply variants too, but
the no-reply *timer* is rarely settable — so landline overflow is flagged
best_effort and omits the timer segment.
"""
from __future__ import annotations

import phonenumbers

from booking_engine.services.phone_normalize import normalize_e164


def _line_type(salon_existing_number: str | None) -> str:
    """'mobile' if the salon's line is a mobile, else 'landline' (safe default)."""
    try:
        parsed = phonenumbers.parse(salon_existing_number or "", "IT")
    except phonenumbers.NumberParseException:
        return "landline"
    if phonenumbers.number_type(parsed) == phonenumbers.PhoneNumberType.MOBILE:
        return "mobile"
    return "landline"


def _timer_seconds(overflow_ring_count: int) -> int:
    # GSM no-reply timer accepts 5..30s in 5s steps; one ring ~= 5s.
    return max(5, min(30, overflow_ring_count * 5))


def build_instructions(
    *,
    kairo_number: str,
    salon_existing_number: str | None,
    answer_mode: str,
    overflow_ring_count: int,
) -> dict:
    """Return both Full and Overflow instruction blocks; the salon picks one."""
    did = normalize_e164(kairo_number) or kairo_number
    line_type = _line_type(salon_existing_number)
    secs = _timer_seconds(overflow_ring_count)

    if line_type == "mobile":
        full = {
            "codes": [f"**21*{did}#"],
            "note": "Dial to send every call to the assistant. Undo with ##21#.",
            "best_effort": False,
        }
        overflow = {
            "codes": [f"**61*{did}*11*{secs}#", f"**67*{did}#", f"**62*{did}#"],
            "note": f"Sends only calls you don't answer within ~{secs}s, or when "
                    "busy/unreachable, to the assistant.",
            "best_effort": False,
        }
    else:
        full = {
            "codes": [f"*21*{did}#"],
            "note": "Most Italian landlines accept this to forward every call. "
                    "If it doesn't work, set unconditional forwarding to the "
                    f"number {did} in your carrier's app or portal.",
            "best_effort": True,
        }
        overflow = {
            "codes": [f"*61*{did}#"],
            "note": "Forwards unanswered calls where supported (the no-answer delay "
                    "is usually fixed by the carrier, not settable). Otherwise set "
                    f"call-forward-on-no-answer to {did} in your carrier's app.",
            "best_effort": True,
        }

    return {
        "line_type": line_type,
        "recommended": answer_mode,
        "full": full,
        "overflow": overflow,
    }
