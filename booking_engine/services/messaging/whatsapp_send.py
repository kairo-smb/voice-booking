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
throughput but nothing anyone can look at.

Consent is re-read at send time, not trusted from enqueue: a queued row can
sit for hours, and a customer who withdraws consent in-store at 11:00 must not
receive the message scheduled for 15:00. Same trust-boundary reasoning as
sms_send.py's re-check.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from twilio.rest import Client

from booking_engine.db import sms_queries
from booking_engine.db import token_basket_queries as tbq
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging.send_credits import send_credits
from booking_engine.services.messaging.sms_send import _has_active_consent
from booking_engine.services.messaging.whatsapp_templates import clean_variable, render
from booking_engine.services.phone_normalize import normalize_e164

logger = logging.getLogger(__name__)

SALON_TZ = ZoneInfo("Europe/Rome")

# Meta list price for a marketing template to an Italian recipient, plus
# Twilio's per-message fee. Used only to pre-check the balance; the status
# callback writes the real price, exactly as the SMS path does.
_ESTIMATED_USD_PER_MESSAGE = 0.0741

# How many claimed messages one tick will attempt. Nothing to do with Meta's
# 80 msg/s throughput — it bounds how long a single tick can run.
MAX_PER_TICK = 200


def spread(
    count: int, now: datetime, *, start_hour: int, end_hour: int
) -> list[datetime]:
    """Evenly space `count` sends across the remaining send window.

    Pure, so the awkward cases are testable without a clock: before the window
    (use the whole of today), inside it (use what's left of today), after it
    (roll to tomorrow). A single message goes at the start of the window it
    lands in, not the middle — the owner clicking "invia" at 09:05 expects
    something to happen this morning.
    """
    if count <= 0:
        return []

    local = now.astimezone(SALON_TZ)
    day_start = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    day_end = local.replace(hour=end_hour, minute=0, second=0, microsecond=0)

    if local >= day_end:
        day_start += timedelta(days=1)
        day_end += timedelta(days=1)
        begin = day_start
    else:
        begin = max(local, day_start)

    window = (day_end - begin).total_seconds()
    # One send per slot; with count == 1 the step is irrelevant and it goes first.
    step = window / count if count > 1 else 0.0
    return [
        (begin + timedelta(seconds=step * i)).astimezone(now.tzinfo or SALON_TZ)
        for i in range(count)
    ]


async def enqueue_campaign(
    *,
    shop_id: UUID,
    campaign_key: str,
    template_key: str,
    recipients: list[dict],
    now: datetime | None = None,
    settings,
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

    cap = int(sender["daily_cap"])
    if len(recipients) > cap:
        return {"ok": False, "error": "over_daily_cap", "daily_cap": cap}

    when = spread(
        len(recipients),
        now or datetime.now(tz=SALON_TZ),
        start_hour=settings.whatsapp_send_start_hour,
        end_hour=settings.whatsapp_send_end_hour,
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
        elif not _has_active_consent(customer):
            reason = "no_consent"

        message_id = await wq.enqueue(
            shop_id=shop_id, customer_id=customer_id, campaign_key=campaign_key,
            to_phone=phone or "", from_number=sender["phone_number"],
            content_sid=template["content_sid"], variables=variables,
            preview=render(template_key, variables), scheduled_at=scheduled_at,
            status="suppressed" if reason else "queued", suppressed_reason=reason,
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


def _twilio_send(
    *, to: str, from_: str, content_sid: str, variables: dict,
    subaccount_sid: str, auth_token: str, status_callback: str | None,
) -> tuple[str, float | None]:
    """Blocking Twilio call, wrapped in a thread by the caller.

    Authenticating as `subaccount_sid` with the *parent's* token is how a
    subaccount is addressed — there is no separate per-salon secret.
    """
    client = Client(subaccount_sid, auth_token)
    msg = client.messages.create(
        to=f"whatsapp:{to}",
        from_=f"whatsapp:{from_}",
        content_sid=content_sid,
        content_variables=json.dumps(variables),
        status_callback=status_callback,
    )
    price = abs(float(msg.price)) if getattr(msg, "price", None) else None
    return msg.sid, price


async def send_due(*, settings) -> dict:
    """Send everything due right now, within each shop's daily cap."""
    counts = {"sent": 0, "suppressed": 0, "failed": 0, "deferred": 0,
              "requeued": await wq.requeue_stuck()}

    claimed = await wq.claim_due(MAX_PER_TICK)
    if not claimed:
        return counts

    senders: dict[UUID, dict] = {}
    remaining: dict[UUID, int] = {}
    status_callback = (
        f"{settings.public_base_url}/api/v1/whatsapp/webhook/status"
        if settings.public_base_url else None
    )

    for msg in claimed:
        shop_id = msg["shop_id"]
        if shop_id not in senders:
            sender = await wq.get_sender(shop_id)
            senders[shop_id] = sender or {}
            remaining[shop_id] = max(
                0, int((sender or {}).get("daily_cap") or 0)
                - await wq.sent_today(shop_id)
            )
        sender = senders[shop_id]

        if remaining[shop_id] <= 0:
            # Over cap: later, not never. Dropping it would silently lose a
            # promotion the owner scheduled and believes is going out.
            await wq.requeue_one(message_id=msg["id"], minutes=60)
            counts["deferred"] += 1
            continue

        # Re-read consent: the row may have been queued hours ago.
        customer = (
            await sms_queries.get_customer_for_send(shop_id, msg["customer_id"])
            if msg["customer_id"] else None
        )
        if msg["customer_id"] and (not customer or not _has_active_consent(customer)):
            await wq.mark_suppressed(message_id=msg["id"], reason="no_consent")
            counts["suppressed"] += 1
            continue

        credits = send_credits(_ESTIMATED_USD_PER_MESSAGE)
        if await tbq.get_balance(shop_id) < credits:
            await wq.mark_suppressed(
                message_id=msg["id"], reason="insufficient_credits"
            )
            counts["suppressed"] += 1
            continue

        variables = msg["variables"]
        if isinstance(variables, str):     # asyncpg hands jsonb back as text
            variables = json.loads(variables)

        try:
            provider_sid, price = await asyncio.to_thread(
                _twilio_send,
                to=msg["to_phone"], from_=msg["from_number"],
                content_sid=msg["content_sid"], variables=variables,
                subaccount_sid=sender["subaccount_sid"],
                auth_token=settings.twilio_auth_token,
                status_callback=status_callback,
            )
        except Exception as exc:  # noqa: BLE001 — provider errors are data
            await wq.mark_failed(message_id=msg["id"], error_code=str(exc))
            counts["failed"] += 1
            continue

        # Debit after the provider accepts, never before — same ordering, and
        # the same reason, as sms_send.py: a Twilio rejection must not leave
        # the shop paying for a message that never went out.
        if not await tbq.try_debit_for_message(
            shop_id=shop_id, credits=credits, whatsapp_message_id=msg["id"]
        ):
            logger.warning(
                "whatsapp.unbilled_send shop=%s message=%s credits=%s",
                shop_id, msg["id"], credits,
            )

        await wq.mark_sent(
            message_id=msg["id"], provider_sid=provider_sid,
            price_usd=price, credits=credits,
        )
        remaining[shop_id] -= 1
        counts["sent"] += 1

    return counts
