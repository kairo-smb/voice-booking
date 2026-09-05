"""Queue personalised WhatsApp marketing, then drip it out across the day.

Two halves, deliberately separated:

- `enqueue_campaign` is synchronous and owner-facing. It gates on consent,
  writes one row per recipient (including the refusals), and stamps each with
  a `scheduled_at` spread across the salon's opening hours.
- `send_due` is the hourly tick's half. It claims what is due, re-checks
  everything that could have changed while the row sat in the queue, and
  sends.

Why the schedule is decided at enqueue time rather than "send N per tick":
the owner can see, immediately, exactly when each customer will be contacted,
and cancelling is just deleting queued rows. A per-tick quota gives the same
throughput but nothing anyone can look at. For a bulk campaign that outlasts
one day's cap, `spread` rolls onto following days rather than piling
everything onto today for `send_due` to defer an hour at a time.

Sending goes straight to Meta's Cloud API — no BSP — and is *paced*: see
`send_due`. Nothing here debits AI credits, because the salon's own card is
on its own WABA and Meta charges it directly.

Consent is re-read at send time, not trusted from enqueue: a queued row can
sit for hours, and a customer who withdraws consent in-store at 11:00 must not
receive the message scheduled for 15:00. Same trust-boundary reasoning as
sms_send.py's re-check.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from booking_engine.clients import meta_whatsapp as meta
from booking_engine.db import sms_queries
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import meta_limits
from booking_engine.services.messaging.pacer import Pacer
from booking_engine.services.messaging.sms_send import _has_active_consent
from booking_engine.services.messaging.whatsapp_pricing import estimate_usd
from booking_engine.services.messaging.whatsapp_templates import clean_variable, render
from booking_engine.services.phone_normalize import normalize_e164

logger = logging.getLogger(__name__)

SALON_TZ = ZoneInfo("Europe/Rome")

# What the salon will be billed by Meta for one marketing template to an
# Italian recipient. Written to the row at send time and never corrected:
# Meta charges the salon directly and reports no amount back to us.
_ESTIMATED_USD_PER_MESSAGE = estimate_usd("marketing")

# How many claimed messages one tick will attempt. Nothing to do with Meta's
# throughput — it bounds how long a single tick can run.
MAX_PER_TICK = 200

# Meta's per-user, cross-brand marketing cap (error 131049). Not an opt-out and
# not a transient failure: the recipient has had enough marketing today, from
# anyone. Meta's own guidance is to wait at least 24h before retrying.
FREQUENCY_CAP_CODE = 131049
FREQUENCY_CAP_RETRY_MINUTES = 24 * 60

# The recipient told Meta to stop hearing from *us*. Permanent.
OPT_OUT_CODE = 131050


def spread(
    count: int, now: datetime, *, start_hour: int, end_hour: int,
    daily_cap: int = 10**6,
) -> list[datetime]:
    """Evenly space `count` sends across the send window, rolling onto more days.

    Pure, so the awkward cases are testable without a clock: before the window
    (use the whole of today), inside it (use what's left of today), after it
    (roll to tomorrow). A single message goes at the start of the window it
    lands in, not the middle — the owner clicking "invia" at 09:05 expects
    something to happen this morning.

    `daily_cap` is what makes bulk work. A 400-recipient campaign against a
    50/day sender is eight days of drip, and laying it all on today would just
    hand `send_due` 350 rows to defer by an hour, over and over, until the
    queue is unreadable and the owner has no idea when anyone gets contacted.
    Chunking here means the schedule the owner is shown at enqueue time is the
    schedule that actually happens.
    """
    if count <= 0:
        return []
    per_day = max(1, daily_cap)

    out: list[datetime] = []
    local = now.astimezone(SALON_TZ)
    day = 0
    while len(out) < count:
        day_start = (local + timedelta(days=day)).replace(
            hour=start_hour, minute=0, second=0, microsecond=0
        )
        day_end = day_start.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        # Today's window may already be over, or partly gone.
        begin = max(local, day_start) if day == 0 else day_start
        if begin >= day_end:
            day += 1
            continue

        n = min(per_day, count - len(out))
        step = (day_end - begin).total_seconds() / n if n > 1 else 0.0
        out += [begin + timedelta(seconds=step * i) for i in range(n)]
        day += 1

    return [d.astimezone(now.tzinfo or SALON_TZ) for d in out]


async def enqueue_campaign(
    *,
    shop_id: UUID,
    campaign_key: str,
    template_key: str,
    recipients: list[dict],
    now: datetime | None = None,
    settings,
    initiated_by: UUID | None = None,
) -> dict:
    """Queue one personalised message per recipient. Refusals are rows, not silence.

    `recipients` is `[{"customer_id": UUID, "variables": {"1": "Giulia", ...}}]`
    — the caller (the webapp, from its LLM copy generator) supplies the
    variable values. It never supplies a body: on WhatsApp there is no body to
    supply, only an approved template plus its variables.
    """
    sender = await wq.get_sender(shop_id)
    if not sender or sender.get("status") != "online":
        return {"ok": False, "error": "sender_not_online"}

    template = await wq.get_template(shop_id, template_key)
    if not template:
        return {"ok": False, "error": "unknown_template"}
    if template["status"] != "approved":
        # Sending on an unapproved template fails at Meta and still burns a
        # queue slot, so refuse here where the owner can see why.
        return {"ok": False, "error": f"template_{template['status']}"}

    # UTILITY is transactional — a reminder about the customer's own
    # appointment. It needs no marketing consent, is not blocked by Meta's +1
    # marketing pause, and must never be suppressed by the 7-day recipient
    # cooldown (that cooldown is our guard against the MARKETING frequency
    # cap; an appointment reminder is not a promotion). Nothing fails when
    # this is wrong the other way: a missing category fails closed to the
    # stricter MARKETING path.
    is_utility = template.get("category") == "UTILITY"

    # The binding daily ceiling: min(Meta's tier, our drip rate). Not
    # `sender["daily_cap"]` directly — that is a commercial knob and must never
    # be able to authorise more than the platform allows. See meta_limits.
    cap = meta_limits.effective_daily_cap(sender)
    if cap <= 0:
        return {"ok": False, "error": "sender_has_no_allowance"}

    # No Kairo-side monthly allowance. As a Meta Tech Provider we have no
    # credit line to share, so Meta bills the salon's own card directly — a
    # ceiling here would protect no margin of ours and only suppress the usage
    # that makes the product stick. Meta's tier (above) is the real limit, and
    # it is one we can read. See the 2026-08-24 campaigns spec §7.3.

    when = spread(
        len(recipients),
        now or datetime.now(tz=SALON_TZ),
        start_hour=settings.whatsapp_send_start_hour,
        end_hour=settings.whatsapp_send_end_hour,
        daily_cap=cap,
    )

    # Who already heard from this salon inside the cooldown. One batched query
    # for the whole campaign — a bulk send is up to 2000 recipients, and this
    # is the safeguard that keeps us *under* Meta's per-user marketing cap
    # rather than discovering it as a 131049 after the send is spent. UTILITY
    # sends skip it entirely: the cooldown guards marketing, and a reminder
    # must not be delayed (or suppressed) by last week's offer.
    cooled_off = (
        set()
        if is_utility
        else await wq.recently_contacted(
            shop_id=shop_id,
            customer_ids=[r["customer_id"] for r in recipients],
            hours=settings.whatsapp_recipient_cooldown_hours,
        )
    )

    queued, suppressed, skipped = 0, 0, 0
    for recipient, scheduled_at in zip(recipients, when):
        customer_id = recipient["customer_id"]
        variables = {
            str(k): clean_variable(str(v))
            for k, v in (recipient.get("variables") or {}).items()
        }
        customer = await sms_queries.get_customer_for_send(shop_id, customer_id)
        phone = normalize_e164((customer or {}).get("phone") or "")

        reason = None
        if not customer:
            reason = "customer_not_found"
        elif not phone:
            reason = "no_phone"
        elif not is_utility and not _has_active_consent(customer):
            reason = "no_consent"
        elif not is_utility and not meta_limits.marketing_allowed(phone):
            # Meta has paused marketing delivery to +1 entirely since
            # 2025-04-01. Refused here rather than queued as a certain failure.
            reason = "marketing_blocked_destination"
        elif not is_utility and customer_id in cooled_off:
            reason = "recently_contacted"

        message_id = await wq.enqueue(
            shop_id=shop_id, customer_id=customer_id, campaign_key=campaign_key,
            to_phone=phone or "", from_number=sender["phone_number"] or "",
            template_name=template["name"], template_language=template["language"],
            variables=variables,
            preview=render(template_key, variables), scheduled_at=scheduled_at,
            status="suppressed" if reason else "queued", suppressed_reason=reason,
            initiated_by=initiated_by,
        )
        if message_id is None:
            skipped += 1        # this campaign already reached them
        elif reason:
            suppressed += 1
        else:
            queued += 1

    return {"ok": True, "queued": queued, "suppressed": suppressed,
            "already_sent": skipped,
            "first_at": when[0].isoformat() if when else None,
            "last_at": when[-1].isoformat() if when else None}


async def send_due(*, settings) -> dict:
    """Send everything due right now, within each shop's daily cap.

    Dispatch is serial and **paced**. Serial because the loop mutates per-shop
    remaining-cap counters as it goes, and making it concurrent would turn
    those into locks for throughput nobody needs. Paced because one tick can
    claim MAX_PER_TICK rows across every tenant at once: Meta's per-number
    ceiling is far above that, but the Graph API's app-level limit is shared
    by all of them and is the one we can actually trip.
    """
    counts = {"sent": 0, "suppressed": 0, "failed": 0, "deferred": 0,
              "rate_capped": 0, "requeued": await wq.requeue_stuck()}

    claimed = await wq.claim_due(MAX_PER_TICK)
    if not claimed:
        return counts

    senders: dict[UUID, dict] = {}
    remaining: dict[UUID, int] = {}
    # Clamped, not trusted: below MAX_SENDS_PER_MINUTE no single number can be
    # driven past even the slowest per-number throughput Meta grants (20 mps,
    # coexistence), however the claimed batch happens to fall across shops.
    # That invariant is what makes a second, per-number pacer unnecessary.
    pacer = Pacer(meta_limits.safe_sends_per_minute(settings.whatsapp_sends_per_minute))

    for msg in claimed:
        shop_id = msg["shop_id"]
        if shop_id not in senders:
            sender = await wq.get_sender(shop_id)
            senders[shop_id] = sender or {}
            # Meta's window, not ours. `sent_today` resets at midnight; the
            # tier is measured over a rolling 24h, so using the calendar day
            # here would hand a capped sender a fresh allowance at 00:00.
            remaining[shop_id] = max(
                0, meta_limits.effective_daily_cap(sender or {})
                - await wq.sent_last_24h(shop_id)
            )
        sender = senders[shop_id]

        if remaining[shop_id] <= 0:
            # Over cap: later, not never. Dropping it would silently lose a
            # promotion the owner scheduled and believes is going out.
            await wq.requeue_one(message_id=msg["id"], minutes=60)
            counts["deferred"] += 1
            continue

        # An 'online' sender always has both — claim_due joins on that status,
        # and both write paths set it only after writing the credentials. Guard
        # anyway: without it a missing column reaches the send as a KeyError,
        # gets caught as a provider error, and marks the message permanently
        # `failed`. A configuration problem must not burn the owner's messages.
        if not sender.get("phone_number_id") or not sender.get("access_token"):
            logger.error("whatsapp.sender_missing_credentials shop=%s", shop_id)
            await wq.requeue_one(message_id=msg["id"], minutes=60)
            counts["deferred"] += 1
            continue

        # Re-read consent and the cooldown only for MARKETING, for the same
        # reason consent is re-read at all: a queued row can sit for hours (or
        # days, on a multi-day drip), and both the customer's consent and the
        # per-user marketing cap can change in between. A UTILITY reminder is
        # exempt from both — it is transactional, about the customer's own
        # appointment, and the cooldown must never suppress it.
        if msg["customer_id"] and msg.get("category") != "UTILITY":
            customer = await sms_queries.get_customer_for_send(
                shop_id, msg["customer_id"],
            )
            if not customer or not _has_active_consent(customer):
                await wq.mark_suppressed(message_id=msg["id"], reason="no_consent")
                counts["suppressed"] += 1
                continue
            if await wq.recently_contacted(
                shop_id=shop_id, customer_ids=[msg["customer_id"]],
                hours=settings.whatsapp_recipient_cooldown_hours,
            ):
                await wq.mark_suppressed(
                    message_id=msg["id"], reason="recently_contacted",
                )
                counts["suppressed"] += 1
                continue

        variables = msg["variables"]
        if isinstance(variables, str):     # asyncpg hands jsonb back as text
            variables = json.loads(variables)

        # No credit check and no debit. As a Meta Tech Provider we have no
        # credit line to share: the salon's own card is on its own WABA and
        # Meta bills it directly, so charging AI credits here would bill the
        # same message twice. The SMS path still debits — there Kairo really
        # does pay Twilio. See CLAUDE.md §2026-08-24.
        await pacer.wait()
        try:
            provider_sid = await meta.send_template(
                phone_number_id=sender["phone_number_id"],
                token=sender["access_token"],
                to=msg["to_phone"],
                name=msg["template_name"],
                language=msg["template_language"],
                variables=variables,
            )
        except meta.MetaError as exc:
            # Three outcomes, not one. Collapsing them (as the Twilio version
            # did) either silences customers who did nothing wrong, or retries
            # something Meta will never accept.
            if exc.code == OPT_OUT_CODE:
                await wq.mark_suppressed(message_id=msg["id"], reason="opted_out")
                if msg["customer_id"]:
                    await wq.withdraw_marketing_consent(msg["customer_id"])
                counts["suppressed"] += 1
            elif exc.code == FREQUENCY_CAP_CODE:
                await wq.requeue_one(
                    message_id=msg["id"], minutes=FREQUENCY_CAP_RETRY_MINUTES
                )
                counts["rate_capped"] += 1
            else:
                await wq.mark_failed(message_id=msg["id"], error_code=str(exc))
                counts["failed"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 — transport errors are data too
            await wq.mark_failed(message_id=msg["id"], error_code=str(exc))
            counts["failed"] += 1
            continue

        await wq.mark_sent(
            message_id=msg["id"], provider_sid=provider_sid,
            price_usd=_ESTIMATED_USD_PER_MESSAGE, credits=None,
        )
        remaining[shop_id] -= 1
        counts["sent"] += 1

    return counts
