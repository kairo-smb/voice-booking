"""A WhatsApp conversation is a session row, and voice_agent.calls already is
one: shop, caller_number, customer, outcome, summary, appointment.

Nothing about booking, authz or constraints is rebuilt. caller_number here is
the customer's WhatsApp number, which Meta has verified — strictly stronger
evidence than a voice call's caller ID, so authorize_booking_change works
unchanged.

The table was never telephony-specific. `twilio_call_sid` is UNIQUE but
nullable and `duration_seconds` is the only voice-only column (migration 24
adds `channel` and a COMMENT saying so). Mint a call token against a row here
and the twelve existing tools answer it exactly as they answer a phone call.
"""
from __future__ import annotations

from uuid import UUID

from booking_engine.db.connection import execute_one
from booking_engine.services.messaging import wa_routing

# The open session for one number at one shop, if there is one.
#
# Anchored on `started_at`, bounded by wa_routing.SESSION_GAP — bound as a
# parameter, never written as `interval '24 hours'`, so there is exactly one
# definition of "one conversation" in the codebase and `session_messages`
# cannot come to disagree with the row it runs inside. asyncpg encodes a
# timedelta straight to interval; a string here is a DataError at bind time.
#
# This is marginally *stricter* than the message-gap rule it borrows the
# constant from: a conversation running continuously for longer than the gap
# is split into a second session rather than merged forever. Erring that way
# starts a fresh request; erring the other way would answer a new question
# with a month-old session's state.
#
# `channel = 'whatsapp'` is not decoration — the same person's voice call from
# the same number is a different conversation, on a channel that closes its
# rows by hanging up.
_FIND = """
SELECT id
  FROM voice_agent.calls
 WHERE shop_id = $1
   AND channel = 'whatsapp'
   AND caller_number = $2
   AND ended_at IS NULL
   AND started_at > now() - $3::interval
 ORDER BY started_at DESC
 LIMIT 1
"""

# duration_seconds is deliberately absent: it is voice-only and NULL is the
# truth for this channel, not zero.
_OPEN = """
INSERT INTO voice_agent.calls
    (shop_id, channel, caller_number, customer_id, customer_match, started_at)
VALUES ($1, 'whatsapp', $2, $3, $4, now())
RETURNING id
"""


async def open_session(
    *, shop_id: UUID, phone: str, customer_id: UUID | None,
) -> UUID:
    """The call id for this message — the conversation's, or a new one.

    `shop_id` is part of the lookup because a phone number is not globally
    unique across tenants: one person can be a customer of two salons, and a
    shared session would hand one shop's agent a token scoped to the other.

    Concurrency: two messages arriving at the same instant can both miss the
    SELECT and open two rows. Accepted rather than locked — WhatsApp delivers
    a person's messages in order, the duplicate costs a second session row and
    no wrong answer, and the partial unique index that would prevent it
    ((shop_id, caller_number) WHERE channel = 'whatsapp' AND ended_at IS NULL)
    would also forbid the legitimate new session after a stale row that was
    never closed.
    """
    # The agent turn loop that drives this session lives in Task 20's repo.
    found = await execute_one(_FIND, shop_id, phone, wa_routing.SESSION_GAP)
    if found:
        return found["id"]
    # Legal values are the CHECK in 03_voice_agent_schema.sql:
    # existing / created / unmatched / ambiguous. Opening a session never
    # creates a customer, so 'created' is not ours to write here.
    match = "existing" if customer_id else "unmatched"
    row = await execute_one(_OPEN, shop_id, phone, customer_id, match)
    return row["id"]
