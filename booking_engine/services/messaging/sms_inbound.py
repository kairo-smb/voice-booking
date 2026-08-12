"""Inbound SMS — opt-out handling.

Twilio's automatic STOP handling covers US/Canada long codes only; an Estonian
number gets none of it, so honouring STOP is entirely our job and is a legal
requirement, not a nicety. See docs/messaging-design.md §6.3.
"""
from __future__ import annotations

import re

from booking_engine.db import sms_queries
from booking_engine.services.phone_normalize import normalize_e164

# Italian and English opt-out words. Matched as the WHOLE message (modulo
# whitespace and punctuation), never as a substring: "non fermatevi, stop mai!"
# is not an opt-out and unsubscribing someone who didn't ask is its own harm.
_STOP_WORDS = {"stop", "alt", "cancella", "cancellami", "unsubscribe", "stopall"}
_STRIP = re.compile(r"^[\s\W_]+|[\s\W_]+$", re.UNICODE)


def parse_stop_keyword(body: str | None) -> str | None:
    """Return the matched keyword as the customer typed it, or None."""
    if not body:
        return None
    cleaned = _STRIP.sub("", body)
    if cleaned.lower() in _STOP_WORDS:
        return cleaned
    return None


async def handle_inbound(*, to_number: str, from_number: str, body: str | None) -> bool:
    """Process one inbound SMS. Returns True when it was an opt-out we acted on."""
    keyword = parse_stop_keyword(body)
    if not keyword:
        return False

    shop_id = await sms_queries.get_shop_by_sender_number(to_number)
    if not shop_id:
        return False

    phone = normalize_e164(from_number) or from_number

    # Two writes, both required. The opt_outs row suppresses even when no
    # customer matches; the consent update keeps the webapp honest.
    await sms_queries.record_opt_out(
        shop_id=shop_id, phone_normalized=phone,
        keyword=keyword.upper(), raw_body=(body or "")[:500],
    )
    await sms_queries.withdraw_marketing_consent(shop_id=shop_id, phone_normalized=phone)
    return True
