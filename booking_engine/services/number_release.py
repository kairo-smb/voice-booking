"""Grace-period release of a shop's Twilio number once its plan lapses.

`billing/webhook/process-event.ts` (webapp) nulls `shops.plan_id` on
cancellation but has no reach into this repo's `voice_agent.shop_telephony` —
so without this module a lapsed shop keeps a number we keep paying ~$3/mo
for, forever (the "Cancellation gap" flagged in CLAUDE.md, 2026-08-14).

Design: a grace period, not an instant release. Releasing the moment a plan
lapses gives no warning and Twilio does not hold a released number for a
resubscribing salon to reclaim. So the hourly tick (`sweep`) schedules a
deadline the first time it sees a number with no plan behind it, clears the
schedule if the plan returns before that deadline, and only releases once
the deadline has actually passed. The deadline is derived from when *we*
first noticed, not a `plan_lapsed_at` column — that schema is
`business_app_core`, owned by the webapp repo, not this one.

See booking_engine/db/sql/13_number_release.sql.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from twilio.base.exceptions import TwilioRestException

from booking_engine.clients import push_notifications
from booking_engine.clients import twilio_numbers
from booking_engine.db import number_request_queries
from booking_engine.db import voice_telephony_queries

logger = logging.getLogger(__name__)

GRACE_DAYS = 14


@dataclass(frozen=True)
class ReleaseInput:
    has_plan: bool
    release_scheduled_at: datetime | None


def decide_release(inp: ReleaseInput, now: datetime) -> tuple[str, datetime | None]:
    """Pure policy: turn (has_plan, existing schedule) into an action.

    Returns (action, deadline):
    - 'none'     — nothing to do; deadline echoes back whatever is already
                    scheduled (or None if nothing is).
    - 'schedule' — first time we've seen this shop with no plan; deadline is
                    now + GRACE_DAYS.
    - 'clear'    — the plan came back before the deadline; wipe the schedule.
    - 'release'  — no plan, and the previously scheduled deadline has passed.

    The has_plan=False/already-scheduled/still-in-the-future case matters
    most: it must return 'none' with the *existing* deadline, not
    re-schedule. Re-scheduling on every hourly tick would push the deadline
    forward by GRACE_DAYS each run and the number would never actually be
    released — see the regression test for this exact case.
    """
    if inp.has_plan:
        if inp.release_scheduled_at is None:
            return ("none", None)
        return ("clear", None)

    if inp.release_scheduled_at is None:
        return ("schedule", now + timedelta(days=GRACE_DAYS))
    if inp.release_scheduled_at > now:
        return ("none", inp.release_scheduled_at)
    return ("release", None)


async def release_for_shop(shop_id: UUID, *, settings, reason: str) -> str:
    """Release one shop's number. Returns 'no_number' | 'released' | 'release_failed'.

    Order is deliberate: Twilio first, local row second. Deleting the
    shop_telephony row before Twilio confirms the number is gone would leave
    us paying for a number we no longer track — the same orphan class this
    codebase already guards against elsewhere (see insert_telephony's
    docstring). A 404 from Twilio means the number is already gone (e.g. a
    previous run released it but crashed before step 3) and is treated as
    success so the local row doesn't outlive it; any other failure leaves
    the row untouched so the next tick retries.
    """
    row = await voice_telephony_queries.get_telephony(shop_id)
    if row is None:
        return "no_number"

    try:
        await asyncio.to_thread(
            twilio_numbers.release_number,
            sid=row["kairo_number_sid"],
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
        )
    except TwilioRestException as exc:
        if exc.status != 404:
            logger.exception(
                "numbers.release_failed shop=%s sid=%s reason=%s",
                shop_id, row["kairo_number_sid"], reason,
            )
            return "release_failed"
        # 404: already gone at Twilio (e.g. a prior run released it and
        # crashed before deleting the row). Fall through — the local row
        # must not outlive the number it claims to reference.
    except Exception:  # noqa: BLE001 — any other failure must not delete the row
        logger.exception(
            "numbers.release_failed shop=%s sid=%s reason=%s",
            shop_id, row["kairo_number_sid"], reason,
        )
        return "release_failed"

    await voice_telephony_queries.delete_telephony(shop_id)

    await number_request_queries.set_status(
        shop_id=shop_id,
        status="released",
        released_at_now=True,
        released_number=row["kairo_number"],
    )

    # push_notifications.send_push is currently a stub that only logs
    # (booking_engine/clients/push_notifications.py). Emitted here for
    # consistency with the existing number_request_approved/rejected
    # emitters so it becomes real the day that stub is wired up.
    await push_notifications.send_push(
        shop_id=shop_id,
        event="number_released",
        payload={"kairo_number": row["kairo_number"], "reason": reason},
    )
    return "released"


async def sweep(*, settings) -> dict:
    """Hourly tick: schedule, clear, or execute a release for every provisioned number.

    One shop's failure must not abort the sweep — each shop is wrapped in
    its own try/except and counted under 'errors' rather than raised.
    """
    counts = {"scheduled": 0, "cleared": 0, "released": 0, "errors": 0}
    now = datetime.now(timezone.utc)

    rows = await number_request_queries.list_provisioned_numbers()
    for row in rows:
        shop_id = row["shop_id"]
        try:
            has_plan = await number_request_queries.has_active_plan(shop_id)
            inp = ReleaseInput(
                has_plan=has_plan,
                release_scheduled_at=row.get("release_scheduled_at"),
            )
            action, deadline = decide_release(inp, now)

            if action == "schedule":
                await number_request_queries.set_release_schedule(
                    shop_id=shop_id, deadline=deadline,
                )
                counts["scheduled"] += 1
            elif action == "clear":
                await number_request_queries.set_release_schedule(
                    shop_id=shop_id, deadline=None,
                )
                counts["cleared"] += 1
            elif action == "release":
                outcome = await release_for_shop(
                    shop_id, settings=settings, reason="grace_period_expired",
                )
                if outcome == "released":
                    counts["released"] += 1
                elif outcome == "release_failed":
                    counts["errors"] += 1
                # 'no_number' shouldn't happen (row came from
                # list_provisioned_numbers) but isn't an error if it does —
                # nothing left to release.
            # action == 'none': still waiting, or nothing scheduled — no-op.
        except Exception:
            logger.exception("number_release.sweep_failed shop_id=%s", shop_id)
            counts["errors"] += 1

    return counts
