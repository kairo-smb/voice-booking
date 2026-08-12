"""Twilio pass-through cost → AI credits.

Deliberately NOT the webapp's rawToUserCredits(): that applies MARGIN = 10 (the
LLM margin) and floors at 1 credit. Both are wrong for sends — the margin here
is 2×, and a floor of 1 would charge for a free WhatsApp service message.
See docs/messaging-design.md §5.1.
"""
from __future__ import annotations

import math

MARGIN = 2
CREDITS_PER_USD = 1000


def send_credits(twilio_usd: float) -> int:
    """Credits to charge the shop for a send that cost us `twilio_usd`."""
    if not math.isfinite(twilio_usd) or twilio_usd <= 0:
        return 0
    return math.ceil(twilio_usd * MARGIN * CREDITS_PER_USD)
