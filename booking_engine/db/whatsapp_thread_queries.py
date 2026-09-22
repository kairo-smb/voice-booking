"""Threads: the read side of two-way WhatsApp, and the rule about speaking.

A "thread" is not a table. It is every message to and from one phone number,
collapsed per phone at read time — `whatsapp.inbound_messages` and
`whatsapp.outbound_messages` are already the whole record, and a third table
holding the same facts would be a second thing to keep true. (Increment B,
which needs genuine per-thread state — agent mode, suspension, who took over —
is when `whatsapp.threads` earns its keep. Not now.)

**The window governs everything here.** Meta permits free-form (non-template)
messages only within 24 hours of the customer's *last inbound message*. It
resets on every customer message; our own sends do not extend it, and neither
does an echo (`outbound_messages.origin = 'phone'`, the owner answering from
the WhatsApp Business App) — that is not a customer message, which is why the
window is computed from `inbound_messages` alone and an echo is invisible to
it. Outside the window a free-form send fails at Meta with `131047`, an opaque
provider error the owner cannot act on, so the check happens **before** the
Graph call, never after.

The window helpers are pure, taking `now` as an argument, the same shape as
`number_health.decide_health` and `number_release.decide_release`: an
off-by-one on this boundary is a message that dies at Meta, and that is not
something to reason about entangled with IO.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from booking_engine.db.connection import execute, execute_one, execute_void
from booking_engine.services.messaging import wa_routing

# Meta's customer service window. Numerically equal to wa_routing.SESSION_GAP
# and deliberately a separate name: one is Meta's rule about sending, the other
# is ours about what counts as one request. They agree today by reasoning, not
# by coincidence, and either could move without the other.
SERVICE_WINDOW = timedelta(hours=24)

# Re-exported, never re-declared. A second literal here would drift the day an
# intent is added to the router, and silently: threads the router considers
# handled would keep asking the owner to handle them.
WHITELIST = wa_routing.WHITELIST


# ------------------------------------------------------------ the pure rules

def window_expires_at(*, last_inbound: datetime | None) -> datetime | None:
    """Meta permits free-form messages for 24h after the customer's LAST
    message. Our own sends do not extend it — only theirs.

    None means the customer has never written: there is no window, and nothing
    to answer.
    """
    return None if last_inbound is None else last_inbound + SERVICE_WINDOW


def window_open(*, last_inbound: datetime | None, now: datetime) -> bool:
    """True while there is still time left. **At exactly 24h it is closed** —
    the comparison is strict (`now < expires`).

    That direction is chosen, not incidental. Closing a hair early costs a
    template send; staying open a hair late costs a 131047 that reaches the
    owner as an unexplained provider failure.
    """
    expires = window_expires_at(last_inbound=last_inbound)
    return expires is not None and now < expires


def needs_attention(thread: dict) -> bool:
    """What puts a thread in 'Da gestire'. The owner should open the Inbox and
    see only this.

    Two ways in, and escalation is the stronger: an agent that named a
    conversation and then handed it back is still handing it back, whatever it
    named it. Everything else is the routing allowlist — unrouted (the model
    was unsure, or refused, or was never asked) and anything outside
    `wa_routing.WHITELIST` both mean a human. Fails toward the human, which is
    the direction that costs nothing but the owner's attention.
    """
    return (thread.get("escalated") is True
            or thread.get("intent") not in WHITELIST)


# ----------------------------------------------------------------- the reads

# The session's routed intent, derived in SQL exactly as `wa_routing`
# derives it in Python: the latest non-NULL intent since the last gap longer
# than SESSION_GAP. Doing it here rather than returning every message to the
# caller keeps the list endpoint one query instead of one per thread; the
# boundary itself is passed in as a parameter from `wa_routing.SESSION_GAP`
# rather than written as a literal interval, so the two cannot disagree about
# what a session is.
_SESSION_INTENT_CTE = """
    gapped AS (
      SELECT from_phone, received_at, intent,
             received_at - lag(received_at)
               OVER (PARTITION BY from_phone ORDER BY received_at) AS gap
        FROM whatsapp.inbound_messages
       WHERE shop_id = $1
    ), session_start AS (
      SELECT from_phone,
             max(received_at) FILTER (WHERE gap > $2::interval) AS started_at
        FROM gapped GROUP BY from_phone
    ), routed AS (
      SELECT DISTINCT ON (g.from_phone) g.from_phone, g.intent
        FROM gapped g JOIN session_start s USING (from_phone)
       WHERE g.intent IS NOT NULL
         AND (s.started_at IS NULL OR g.received_at >= s.started_at)
       ORDER BY g.from_phone, g.received_at DESC
    )
"""


async def thread_list(shop_id: UUID) -> list[dict]:
    """One row per phone the shop has heard from, newest first.

    **Keyed on inbound**, so a customer who was only ever messaged by a
    campaign and never replied does not appear: there is no window on that
    phone and nothing there to answer. The outbound side is a LEFT JOIN, for
    "when did we last say anything".

    Phones are matched with the leading '+' stripped on both sides: Meta
    reports `from` as bare E.164 while `to_phone` is whatever the webapp held,
    usually with the plus. The inbound spelling is what comes back as `phone`,
    because a thread only exists at all because of an inbound message.

    `intent` is the *session's* verdict, not the last message's — see
    `_SESSION_INTENT_CTE` — so the caller can hand each row straight to
    `needs_attention`.
    """
    return await execute(
        f"""
        WITH {_SESSION_INTENT_CTE},
        last_in AS (
          SELECT DISTINCT ON (ltrim(from_phone, '+'))
                 ltrim(from_phone, '+') AS key,
                 from_phone              AS phone,
                 received_at             AS last_inbound,
                 customer_id,
                 coalesce(nullif(transcript, ''), body) AS last_message,
                 message_type
            FROM whatsapp.inbound_messages
           WHERE shop_id = $1
           ORDER BY ltrim(from_phone, '+'), received_at DESC
        ), unread AS (
          SELECT ltrim(from_phone, '+') AS key,
                 count(*) FILTER (WHERE read_at IS NULL) AS unread
            FROM whatsapp.inbound_messages
           WHERE shop_id = $1
           GROUP BY 1
        ), last_out AS (
          SELECT ltrim(to_phone, '+') AS key,
                 max(sent_at) AS last_outbound
            FROM whatsapp.outbound_messages
           WHERE shop_id = $1 AND sent_at IS NOT NULL
           GROUP BY 1
        ), escalations AS (
          -- The newest WhatsApp session per phone, and whether the agent gave
          -- it back. `needs_attention` has always read `escalated`; until the
          -- agent existed nothing wrote it, so nothing supplied it here either.
          -- Without this an escalated thread whose intent is still 'booking'
          -- would read as handled — in the allowlist, therefore not the
          -- owner's problem — which is the exact thread most needing them.
          SELECT DISTINCT ON (ltrim(caller_number, '+'))
                 ltrim(caller_number, '+') AS key,
                 (outcome = 'escalated')   AS escalated
            FROM voice_agent.calls
           WHERE shop_id = $1 AND channel = 'whatsapp'
           ORDER BY ltrim(caller_number, '+'), started_at DESC
        )
        SELECT li.phone,
               li.customer_id,
               li.last_inbound,
               li.last_message,
               li.message_type,
               u.unread,
               lo.last_outbound,
               li.last_inbound + $3::interval AS window_expires_at,
               r.intent,
               coalesce(e.escalated, false) AS escalated
          FROM last_in li
          JOIN unread u USING (key)
          LEFT JOIN last_out lo USING (key)
          LEFT JOIN routed r ON ltrim(r.from_phone, '+') = li.key
          LEFT JOIN escalations e USING (key)
         ORDER BY li.last_inbound DESC
        """,
        # Both boundaries are bound as parameters, not written as literal
        # intervals: the session gap is `wa_routing`'s own constant, so the two
        # cannot come to disagree about what one conversation is. asyncpg
        # encodes a timedelta straight to interval — a string here is a
        # DataError at bind time, not a cast.
        shop_id, wa_routing.SESSION_GAP, SERVICE_WINDOW,
    )


async def thread_timeline(shop_id: UUID, phone: str) -> list[dict]:
    """Both halves of one conversation, oldest first.

    `direction` is 'in'/'out' and `origin` is carried on the outbound rows so
    the UI can badge what the owner sent from their own phone ('phone') apart
    from what we sent ('kairo').

    Inbound text is `coalesce(transcript, body)`: a voice note must read as its
    words, not as an empty bubble. `body` stays the raw fact underneath.
    """
    return await execute(
        """
        SELECT 'in' AS direction,
               i.id,
               i.received_at AS at,
               coalesce(nullif(i.transcript, ''), i.body) AS text,
               i.message_type,
               NULL::text AS origin,
               NULL::text AS status,
               i.intent,
               i.read_at
          FROM whatsapp.inbound_messages i
         WHERE i.shop_id = $1 AND ltrim(i.from_phone, '+') = ltrim($2, '+')
        UNION ALL
        SELECT 'out' AS direction,
               o.id,
               coalesce(o.sent_at, o.created_at) AS at,
               o.preview AS text,
               'text' AS message_type,
               o.origin,
               o.status,
               NULL::text AS intent,
               NULL::timestamptz AS read_at
          FROM whatsapp.outbound_messages o
         WHERE o.shop_id = $1 AND ltrim(o.to_phone, '+') = ltrim($2, '+')
           AND o.status NOT IN ('suppressed', 'cancelled')
         ORDER BY at ASC, direction ASC
        """,
        shop_id, phone,
    )


async def mark_read(shop_id: UUID, phone: str) -> int:
    """Clear the unread flag on everything the customer sent. Returns how many.

    Idempotent by construction: the `read_at IS NULL` guard means a second call
    updates nothing and does not move the timestamp of the first.
    """
    rows = await execute(
        """
        UPDATE whatsapp.inbound_messages
           SET read_at = now()
         WHERE shop_id = $1
           AND ltrim(from_phone, '+') = ltrim($2, '+')
           AND read_at IS NULL
        RETURNING id
        """,
        shop_id, phone,
    )
    return len(rows)


async def last_inbound_at(shop_id: UUID, phone: str) -> datetime | None:
    """The one timestamp the window check needs. None = they never wrote."""
    row = await execute_one(
        """
        SELECT max(received_at) AS last_inbound
          FROM whatsapp.inbound_messages
         WHERE shop_id = $1 AND ltrim(from_phone, '+') = ltrim($2, '+')
        """,
        shop_id, phone,
    )
    return row["last_inbound"] if row else None


async def record_reply(
    *, shop_id: UUID, to_phone: str, body: str, provider_sid: str | None,
    origin: str = "kairo",
) -> dict | None:
    """A free-form reply we sent, on the same trail as every other outbound.

    `template_name` and `campaign_key` are NULL, deliberately: this is neither
    a template nor part of a campaign, so the campaign idempotency index (which
    is partial on both being present) does not apply and the same text may be
    sent twice if the owner means to.

    `origin` defaults to 'kairo' — the owner typing in the webapp, as opposed to
    the 'phone' echoes they send from the Business App. The booking agent passes
    **'agent'** (migration 25), and that distinction is load-bearing rather than
    decorative: `wa_session_queries.session_state` reads 'kairo'/'phone' as "a
    human took this thread over, stand down". An agent recording its own replies
    as 'kairo' would read its own last message as the owner arriving and go
    silent after one turn.

    Lands as `sent` with `sent_at = now()`: Meta has already accepted it by the
    time this is called, and the delivery webhook will move it on from there.
    `from_number` is read off the sender row rather than passed in, so a caller
    cannot record a reply as coming from a number the shop does not own.
    """
    return await execute_one(
        """
        INSERT INTO whatsapp.outbound_messages
          (shop_id, to_phone, from_number, preview, provider_sid,
           origin, status, sent_at)
        VALUES ($1, $2,
                coalesce((SELECT phone_number FROM whatsapp.senders
                           WHERE shop_id = $1), ''),
                $3, $4, $5, 'sent', now())
        RETURNING *
        """,
        shop_id, to_phone, body, provider_sid, origin,
    )


# ------------------------------------------------- what the inbound worker needs

async def inbound_history(shop_id: UUID, phone: str) -> list[dict]:
    """Ascending by received_at — oldest first, which is what
    `wa_routing.session_messages` expects.

    Bounded to a recent window: a session cannot span more than SESSION_GAP of
    silence, so anything older can never be part of the current one. Seven days
    rather than one so the boundary is visibly inside the slice — the caller
    needs to *see* the gap that ended the previous session, not to have it
    truncated away.
    """
    return await execute(
        """
        SELECT id, received_at, intent, confidence, message_type
          FROM whatsapp.inbound_messages
         WHERE shop_id = $1
           AND ltrim(from_phone, '+') = ltrim($2, '+')
           AND received_at > now() - interval '7 days'
         ORDER BY received_at ASC
        """,
        shop_id, phone,
    )


async def set_transcript(message_id: UUID, transcript: str) -> None:
    """A voice note's words, stored **beside** the raw message, never over it.

    `body` is what Meta delivered — empty, for audio — and stays that way: it
    is the record of what arrived, and the thread reads
    `coalesce(nullif(transcript, ''), body)` to show the words instead. Called
    only for a non-empty transcript: NULL means "we do not know", which is the
    truth for a note we could not fetch or could not transcribe.
    """
    await execute_void(
        "UPDATE whatsapp.inbound_messages SET transcript = $2 WHERE id = $1",
        message_id, transcript,
    )


async def set_verdict(message_id: UUID, verdict: dict, decision) -> None:
    """The classifier's answer, stored on the message that triggered it.

    `intent` is written only when the decision routed or named a human's
    reason — a **'menu' decision leaves it NULL**, because the session is still
    unrouted and `wa_routing.routed_intent()` must keep saying so. Writing an
    intent there would make the session look named, the worker would skip the
    classifier on the customer's next message, and the menu we just sent would
    be answered by nobody.
    """
    await execute_void(
        """
        UPDATE whatsapp.inbound_messages
           SET intent = $2, confidence = $3, summary = $4
         WHERE id = $1
        """,
        message_id,
        decision.intent if decision.action in ("route", "human") else None,
        _as_float(verdict.get("confidence")),
        verdict.get("summary"),
    )


def _as_float(value) -> float | None:
    """`confidence` comes out of a model's JSON and the column is numeric.

    A word, a bool or a missing key must not raise on the way to the database:
    the verdict has already been acted on by the time this runs, and losing the
    whole row over an unparseable number would lose the intent with it.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
