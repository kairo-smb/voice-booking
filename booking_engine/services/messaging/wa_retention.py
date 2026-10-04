"""How long a WhatsApp conversation is kept, and the sweep that enforces it.

The two views this ships beside (`booking_engine/db/sql/26_whatsapp_retention.sql`)
are the *reason* to keep anything at all: `whatsapp.interaction_history` pairs
the router's verdict with how the session ended, and
`whatsapp.routing_corrections` harvests the disambiguation menu's taps as a
human-labelled eval set. Neither is a copy of anything — they read the rows
this module deletes, so the window below is also the window on both of them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from booking_engine.db import whatsapp_queries as wq

logger = logging.getLogger(__name__)

# Owner's number, 2026-09-21: six months of interactions, kept for post-mortems
# and for improving the routing prompt. It is also the first retention policy
# this feature has had — before it, message bodies accumulated forever, which
# the design doc flagged as an open GDPR gap and this closes.
RETENTION = timedelta(days=183)

# How much one run may remove, counted in **threads** rather than rows.
#
# Two things decide this number. The unit that must not be split is the
# conversation, so the batch has to be whole threads (see `_PURGE`): a limit on
# rows could fill up mid-conversation and leave the other half behind until the
# next run. And the first sweep over an accumulated backlog must not hold row
# locks on the two busiest tables in the schema for minutes — a thread is a
# handful of messages, so 500 of them is a few thousand rows, which is a
# sub-second statement on any of them.
#
# Catch-up is therefore a matter of ticks, not of one long run: at 500 threads
# an hour a year of backlog drains in days, while every individual statement
# stays small enough to be invisible to anything else using the table.
BATCH_THREADS = 500


def is_expired(*, at: datetime, now: datetime) -> bool:
    """Whether a message from `at` has outlived the policy.

    Pure: two timestamps in, a bool out, `now` an argument — the same shape as
    `wa_nudge.should_nudge` and `number_release.decide_release`.

    **At exactly RETENTION it is expired**, and that direction is chosen. A row
    that has reached six months has had its six months, and between the two
    available off-by-ones the one that deletes is the one a retention policy
    exists for. Note this is the opposite lean to `window_open`'s strictness,
    for the opposite reason: there, a hair late is a Meta error; here, a hair
    late is personal data kept past its policy.
    """
    return now - at >= RETENTION


def cutoff(now: datetime) -> datetime:
    """The instant the sweep cuts at. Pure, and the SQL mirror of `is_expired`:
    the statement's predicate is `<= cutoff`, which is `>= RETENTION` old."""
    return now - RETENTION


async def sweep() -> dict:
    """Hourly tick: remove up to BATCH_THREADS threads' worth of expired messages.

    Both directions of a thread go in one statement, so a conversation is never
    left halved — half a conversation is still personal data and is no longer
    readable as a conversation. `voice_agent.calls` is untouched: that row is
    the business record of an appointment being made, it outlives the chat that
    produced it, and it is not this policy's to expire.

    Never raises. It is a tick stage, and one bad run must be counted under
    `errors` rather than 500 the whole tick — the same posture as
    `number_release.sweep` and every WhatsApp stage beside it.
    """
    counts = {"threads": 0, "inbound": 0, "outbound": 0, "errors": 0}
    try:
        result = await wq.purge_expired_threads(
            cutoff=cutoff(datetime.now(timezone.utc)), threads=BATCH_THREADS,
        )
        counts.update({k: int(result.get(k) or 0)
                       for k in ("threads", "inbound", "outbound")})
        if counts["threads"]:
            logger.info(
                "whatsapp.retention_purged threads=%s inbound=%s outbound=%s",
                counts["threads"], counts["inbound"], counts["outbound"],
            )
    except Exception:  # noqa: BLE001 — a tick stage counts, it does not raise
        logger.exception("whatsapp.retention_sweep_failed")
        counts["errors"] = 1
    return counts
