"""One last free message inside the 24h window, inviting the customer to write.

Meta's customer service window permits free-form (non-template) messages only
within 24 hours of the customer's *last* message, and it resets every time
**they** write. So the rule does not forbid answering a conversation; it forbids
**us speaking first** after 24h of their silence. That needs an approved
template — a Meta review, a paid conversation, and a body written weeks before
anyone knew what this thread was about.

The cheap mitigation is to say the last thing while we still may. A single
invitation sent at 20h costs nothing (we are inside the window), needs no
template, and their reply is what reopens the window for another 24 hours. It
does not *close* the hole — a customer who never answers is still unreachable
at 24h — but it converts most of it into an ordinary conversation.

Everything about who may be nudged is in `should_nudge`, which is pure: a dict
in, a bool out, `now` as an argument, no clock and no database. Same shape as
`wa_routing.decide`, `wa_agent.may_speak` and `number_health.decide_health`.
The boundary is the expensive part here — a nudge one minute late is a 131047,
an opaque provider error the owner cannot act on — and that is the last thing
to reason about entangled with IO.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from booking_engine.clients import meta_whatsapp as meta
from booking_engine.db import whatsapp_queries as wq
from booking_engine.db import whatsapp_thread_queries as tq

logger = logging.getLogger(__name__)

# We cannot speak first after 24h of customer silence — that needs an approved
# template. A last free message inside the window invites the customer to write
# back, and their reply is what reopens it. The cheapest mitigation there is.
NUDGE_AFTER_HOURS = 20
NUDGE_AFTER = timedelta(hours=NUDGE_AFTER_HOURS)

# Warm, Italian, and deliberately silent about windows and templates: the
# customer is not party to Meta's rules and a message that explains them reads
# like a machine apologising for itself.
#
# It is also the **marker**. `list_nudge_candidates` matches an
# origin='agent' row on exactly this text to answer "have we already nudged
# this silence?", so the constant is bound as a query parameter and never
# retyped. See `sweep` for why the fact lives on a row rather than in memory.
NUDGE_BODY = "Se ti serve ancora una mano, scrivimi pure."


def should_nudge(thread: dict, *, now: datetime) -> bool:
    """Whether this thread gets its one invitation, right now.

    Pure. Read with `.get`, not `[]`, for the same reason `may_speak` is: a
    thread dict missing a key must produce silence, not a KeyError inside a
    background sweep with nobody to raise to.

    The four timestamps are raw facts — the newest of each kind, over all time —
    and every rule that uses one compares it to `last_inbound`. That is what
    scopes them to *this* silence: an owner who replied last month, or a nudge
    sent before the customer's most recent message, must not silence us forever.
    """
    if not thread.get("agent_enabled"):
        return False
    if thread.get("escalated"):
        return False

    last_inbound = thread.get("last_inbound")
    if last_inbound is None:
        return False       # they have never written: there is no window at all

    # A person is on it. Webapp ('kairo') or the owner's own phone ('phone') —
    # same rule, and the same one `may_speak` applies to speaking at all.
    human = thread.get("human_replied_at")
    if human is not None and human >= last_inbound:
        return False

    # The agent must be the one waiting. If it never spoke, nothing is pending;
    # if the customer wrote after it did, the thread is waiting on *us*, and
    # "scrivimi pure" to someone who just did is nonsense. A tie counts as
    # theirs: a message at least as new as our answer is not our turn to chase.
    agent_at = thread.get("last_agent_at")
    if agent_at is None or agent_at <= last_inbound:
        return False

    # At most once per silence. Durable because it is read off a row — see
    # `sweep`. Scoped to `last_inbound` so a customer who replies and goes
    # quiet again starts a new silence, which may be invited again.
    nudged = thread.get("last_nudge_at")
    if nudged is not None and nudged >= last_inbound:
        return False

    # The window, by the one definition this codebase has. Strict on the far
    # side (`now < expires`), so at exactly 24h it is already closed: closing a
    # hair early costs nothing, staying open a hair late costs a 131047.
    if not tq.window_open(last_inbound=last_inbound, now=now):
        return False

    return now - last_inbound >= NUDGE_AFTER


async def sweep() -> dict:
    """Hourly tick: send the one nudge to every thread that has earned it.

    **"At most once" is a row, not a variable.** The nudge is recorded like any
    other agent reply — `origin='agent'`, `preview = NUDGE_BODY` — and
    `list_nudge_candidates` reads that back as `last_nudge_at`. No column was
    added: Task 21 established the same preference when it spelled the agent as
    an `origin` value rather than a new flag, and a marker derived from the row
    that already records the send cannot drift from it, cannot be forgotten on
    a code path that sends without updating a counter, and survives a restart
    for free.

    The marker is the body text, which is the one thing about this that could
    go stale: if the copy is ever changed, threads mid-silence at that moment
    stop matching and may be invited a second time. That is the cheapest
    failure available here — one extra warm sentence — and it is bounded to the
    deploy. The alternative, a `nudged_at` column, buys immunity to a copy edit
    at the price of a second fact about the same send.

    Order is send-then-record, as in `wa_agent._say`: Meta has delivered it by
    the time the row is written, and re-sending would be the worse of the two
    wrongs. A failed record is logged loudly and costs at most one repeat nudge
    on the next tick.

    One thread's failure must not abort the sweep — each is wrapped and counted
    under 'errors', the same shape as `number_release.sweep`.
    """
    counts = {"nudged": 0, "errors": 0}
    now = datetime.now(timezone.utc)

    for thread in await tq.list_nudge_candidates(NUDGE_BODY):
        phone = str(thread.get("phone") or "")
        try:
            if not should_nudge(thread, now=now):
                continue

            sender = await wq.get_sender(thread["shop_id"])
            if (not sender or sender.get("status") != "online"
                    or not sender.get("phone_number_id")
                    or not sender.get("access_token")):
                # Not a failure: an offline or half-onboarded sender has
                # nothing to send with, and the window will close on its own.
                logger.info("whatsapp.nudge_unsendable shop=%s",
                            thread.get("shop_id"))
                continue

            sid = await meta.send_text(
                phone_number_id=str(sender["phone_number_id"]),
                to=phone, body=NUDGE_BODY, token=str(sender["access_token"]),
            )
            await tq.record_reply(
                shop_id=thread["shop_id"], to_phone=phone, body=NUDGE_BODY,
                provider_sid=sid, origin="agent",
            )
            counts["nudged"] += 1
            logger.info("whatsapp.nudged shop=%s phone=%s sid=%s",
                        thread.get("shop_id"), phone, sid)
        except Exception:  # noqa: BLE001 — one thread must not abort the sweep
            logger.exception("whatsapp.nudge_failed shop=%s phone=%s",
                             thread.get("shop_id"), phone)
            counts["errors"] += 1

    return counts
