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

from booking_engine.db.connection import execute, execute_one, execute_void
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


# ------------------------------------------------- what may_speak needs to know

# Everything the handover rules read, as one query keyed on the session row.
#
# All three facts are DERIVED from rows that already exist — no per-thread state
# table, no `suspended` flag to keep true. The session row says when it started;
# `outbound_messages.origin` says who wrote each reply since.
#
# **`human_replied_at` is the whole point of migration 25's 'agent' origin.**
# The owner writes from the webapp ('kairo') and from the WhatsApp Business App
# on their own phone ('phone'); the agent writes 'agent'. Reading 'kairo' alone
# as "a human took over" was impossible before, because the agent's own replies
# landed there too — it would have read its own last message as the owner
# arriving and gone quiet after a single turn.
#
# `template_name IS NULL AND campaign_key IS NULL` excludes a marketing template
# that happens to land mid-session: a drip campaign firing at a thread is not a
# person choosing to answer it, and treating it as one would silence the agent
# for a reason the owner never chose. Same predicate `record_reply` writes by.
#
# Suppressed and cancelled rows are excluded throughout: a message that never
# left is not a reply and is not a turn, and counting one would silence the
# agent over something the customer never saw.
_SESSION_STATE = """
SELECT c.started_at,
       (c.outcome = 'escalated')                            AS escalated,
       (SELECT count(*)
          FROM whatsapp.outbound_messages o
         WHERE o.shop_id = c.shop_id
           AND ltrim(o.to_phone, '+') = ltrim($2, '+')
           AND o.origin = 'agent'
           AND o.status NOT IN ('suppressed', 'cancelled')
           AND coalesce(o.sent_at, o.created_at) >= c.started_at) AS agent_turns,
       (SELECT max(coalesce(o.sent_at, o.created_at))
          FROM whatsapp.outbound_messages o
         WHERE o.shop_id = c.shop_id
           AND ltrim(o.to_phone, '+') = ltrim($2, '+')
           AND o.origin IN ('kairo', 'phone')
           AND o.template_name IS NULL
           AND o.campaign_key IS NULL
           AND o.status NOT IN ('suppressed', 'cancelled')
           AND coalesce(o.sent_at, o.created_at) >= c.started_at
           -- An echo of a message WE sent is not the owner answering.
           --
           -- Meta's `smb_message_echoes` is documented as mirroring what the
           -- owner sent from the WhatsApp Business App, and Cloud API sends
           -- are understood not to come back on it. That understanding is
           -- **unverified against a real WABA** — no live Meta call has ever
           -- been made from this repo — and if it is wrong the consequence is
           -- silent and total: `record_echo` writes origin='phone', this
           -- column reads the agent's own reply as a human arriving, and the
           -- agent goes quiet after exactly one turn on every thread forever.
           --
           -- Both paths put the wamid in provider_sid (`record_reply` from
           -- `send_text`'s return, `record_echo` from the echo payload), so
           -- the two can be matched. Cheap insurance against a contract we
           -- cannot check until the first real onboarding.
           AND NOT EXISTS (
             SELECT 1 FROM whatsapp.outbound_messages mine
              WHERE mine.origin = 'agent'
                AND mine.provider_sid IS NOT NULL
                AND mine.provider_sid = o.provider_sid
           )) AS human_replied_at
  FROM voice_agent.calls c
 WHERE c.id = $1
"""


async def session_state(*, call_id: UUID, phone: str) -> dict:
    """`started_at`, `escalated`, `agent_turns`, `human_replied_at` for one session.

    Returned as a plain dict so the caller can hand it straight to
    `wa_agent.may_speak`, which is pure and must stay that way. A missing
    session row yields the *most restrictive* reading — escalated, turns
    exhausted — because a turn we cannot establish the state of is not one to
    run on the salon's basket.
    """
    row = await execute_one(_SESSION_STATE, call_id, phone)
    if row is None:
        return {"started_at": None, "escalated": True,
                "agent_turns": 0, "human_replied_at": None}
    return dict(row)


async def mark_escalated(*, call_id: UUID, reason: str) -> None:
    """Hand the session to a person, durably.

    Written on `voice_agent.calls` rather than a WhatsApp-only flag: 'escalated'
    is already in that column's CHECK and already what the voice agent writes
    when it gives up (`voice_tools_lifecycle`), so the Inbox has one vocabulary
    for "a human is needed" across both channels.

    `summary` is left alone — the agent may have written one — and the reason
    lands in `outcome_reason`, which is what Task 24 renders to the owner.
    """
    await execute_void(
        """
        UPDATE voice_agent.calls
           SET outcome = 'escalated', outcome_reason = $2
         WHERE id = $1
        """,
        call_id, reason,
    )


async def session_transcript(
    *, shop_id: UUID, phone: str, since, limit: int = 40,
) -> list[dict]:
    """The conversation so far, as `{role, content}`, oldest first.

    Both halves of the thread since the session opened. Inbound is the customer
    ('user'); everything we sent is 'assistant' — the agent's own replies and,
    when a human wrote before standing the agent down, theirs too. That is the
    truthful framing for a model reading the thread: from the customer's side
    the salon speaks with one voice, whoever was holding the keyboard.

    A voice note reads as its words (`transcript`), never as an empty bubble.
    Templates and campaign sends are excluded — a promo that landed mid-thread
    is not part of this request and would only mislead the turn.

    `limit` is a guard on the prompt, not on the conversation: a session is
    over long before 40 messages, so this only ever bites on a thread that
    somehow ran away.
    """
    return await execute(
        """
        SELECT * FROM (
            SELECT 'user' AS role,
                   coalesce(nullif(i.transcript, ''), i.body) AS content,
                   i.received_at AS at
              FROM whatsapp.inbound_messages i
             WHERE i.shop_id = $1
               AND ltrim(i.from_phone, '+') = ltrim($2, '+')
               AND i.received_at >= $3
            UNION ALL
            SELECT 'assistant' AS role,
                   o.preview AS content,
                   coalesce(o.sent_at, o.created_at) AS at
              FROM whatsapp.outbound_messages o
             WHERE o.shop_id = $1
               AND ltrim(o.to_phone, '+') = ltrim($2, '+')
               AND o.template_name IS NULL
               AND o.campaign_key IS NULL
               AND o.status NOT IN ('suppressed', 'cancelled')
               AND coalesce(o.sent_at, o.created_at) >= $3
        ) t
         WHERE coalesce(t.content, '') <> ''
         ORDER BY t.at ASC
         LIMIT $4
        """,
        shop_id, phone, since, limit,
    )
