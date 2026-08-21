"""Onboard one salon onto WhatsApp: subaccount -> WABA -> sender -> templates.

The shape is forced by two hard rules, not chosen:

1. **One WABA per Twilio account.** So a salon's WhatsApp Business Account
   cannot share Kairo's account with every other salon — each gets a Twilio
   subaccount. (Same audited "don't reuse your own business identity for a
   customer" rule as the regulatory bundles: CLAUDE.md, 2026-08-14.)
2. **The first WABA must be created by the salon itself, in Meta's Embedded
   Signup popup.** There is no server-side API that creates a WABA on a
   customer's behalf. So onboarding is necessarily two round-trips: we
   prepare (`start`), the salon completes Meta's popup in the webapp, the
   webapp hands us back the `waba_id` (`attach_waba`).

Number choice, both supported:

- `source='kairo'` — reuse the number the salon already answers calls on
  (`voice_agent.shop_telephony.kairo_number`). Already bought, already
  SMS-capable, already under the salon's own regulatory bundle: no second
  number, no second $3/mo, no bundle inside the subaccount. Meta's ownership
  OTP arrives as an SMS to a number *we* control, so we catch it ourselves —
  the salon never sees a code.
- `source='salon'` — the salon's own number. It must not already be live on
  WhatsApp (they have to delete the WhatsApp/WhatsApp Business App account on
  it first); Meta's OTP goes to them and they type it into the webapp.
"""
from __future__ import annotations

import logging
import re
from uuid import UUID

from booking_engine.clients import twilio_whatsapp as twa
from booking_engine.db import voice_telephony_queries, whatsapp_queries as wq
from booking_engine.services.messaging.whatsapp_templates import CATALOGUE
from booking_engine.services.phone_normalize import normalize_e164

logger = logging.getLogger(__name__)

# Twilio sender status -> our own. Anything unrecognised stays 'verifying' so
# a new Twilio state can never silently mark a sender ready to send.
_STATUS = {
    "ONLINE": "online",
    "OFFLINE": "offline",
    "VERIFYING": "verifying",
    "CREATING": "verifying",
    "PENDING_VERIFICATION": "verifying",
    "FAILED": "failed",
}


def _otp_url(settings) -> str:
    return f"{settings.public_base_url}/api/v1/whatsapp/webhook/otp"


async def start(
    *, shop_id: UUID, display_name: str, source: str,
    phone_number: str | None, settings,
) -> dict:
    """Create the salon's Twilio subaccount and return its Embedded Signup config.

    Idempotent: an existing subaccount is reused rather than a second one
    created, because a stray subaccount is invisible from this repo and
    nothing would ever clean it up.
    """
    if source not in ("kairo", "salon"):
        return {"ok": False, "error": "invalid_source"}

    if source == "kairo":
        telephony = await voice_telephony_queries.get_telephony(shop_id)
        if not telephony or not telephony.get("kairo_number"):
            return {"ok": False, "error": "no_kairo_number"}
        resolved = telephony["kairo_number"]
    else:
        resolved = normalize_e164(phone_number or "")
        if not resolved:
            return {"ok": False, "error": "invalid_phone"}

    existing = await wq.get_sender(shop_id)
    if existing and existing.get("status") == "online":
        return {"ok": True, "status": "online", "phone_number": existing["phone_number"]}

    row = await wq.upsert_sender(
        shop_id=shop_id, display_name=display_name, source=source
    )
    subaccount_sid = row.get("subaccount_sid")
    if not subaccount_sid:
        sub = await twa.create_subaccount(
            friendly_name=f"kairo-shop-{shop_id}",
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
        )
        subaccount_sid = sub.sid
        await wq.set_sender_fields(
            shop_id,
            subaccount_sid=sub.sid,
            subaccount_auth_token=sub.auth_token,
        )

    await wq.set_sender_fields(shop_id, phone_number=resolved, status="pending_signup")

    return {
        "ok": True,
        "status": "pending_signup",
        "phone_number": resolved,
        # The webapp needs all three to open Meta's popup. Served from here so
        # there is one source of truth for the Meta app identifiers.
        "signup": {
            "app_id": settings.meta_app_id,
            "config_id": settings.meta_config_id,
            "phone_number": resolved,
            "business_name": display_name,
        },
    }


async def attach_waba(*, shop_id: UUID, waba_id: str, settings) -> dict:
    """Register the sender against the WABA the salon just created in Meta."""
    row = await wq.get_sender(shop_id)
    if not row or not row.get("subaccount_sid") or not row.get("phone_number"):
        return {"ok": False, "error": "not_started"}
    if row.get("status") == "online":
        return {"ok": True, "status": "online"}

    await wq.set_sender_fields(shop_id, waba_id=waba_id)

    sender = await twa.register_sender(
        subaccount_sid=row["subaccount_sid"],
        auth_token=settings.twilio_auth_token,
        phone_number=row["phone_number"],
        waba_id=waba_id,
        display_name=row["display_name"],
        callback_url=f"{settings.public_base_url}/api/v1/whatsapp/webhook/inbound",
        status_callback_url=f"{settings.public_base_url}/api/v1/whatsapp/webhook/status",
    )
    await wq.set_sender_fields(
        shop_id,
        sender_sid=sender.sid,
        status=_STATUS.get(sender.status, "verifying"),
        quality_rating=sender.quality_rating,
        messaging_limit=sender.messaging_limit,
    )

    # A Kairo-owned number can't type its own OTP in. Bind its inbound-SMS
    # webhook so we catch Meta's code, and unbind it again in `_finalize`.
    if row["source"] == "kairo":
        telephony = await voice_telephony_queries.get_telephony(shop_id)
        if telephony and telephony.get("kairo_number_sid"):
            await twa.set_sms_webhook(
                number_sid=telephony["kairo_number_sid"],
                sms_url=_otp_url(settings),
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
            )

    return {"ok": True, "status": _STATUS.get(sender.status, "verifying"),
            "sender_sid": sender.sid}


async def submit_code(*, shop_id: UUID, code: str, settings) -> dict:
    """Hand Meta's ownership OTP to Twilio. Same call for both number paths."""
    row = await wq.get_sender(shop_id)
    if not row or not row.get("sender_sid"):
        return {"ok": False, "error": "not_registered"}

    sender = await twa.verify_sender(
        sender_sid=row["sender_sid"], code=code,
        subaccount_sid=row["subaccount_sid"],
        auth_token=settings.twilio_auth_token,
    )
    status = _STATUS.get(sender.status, "verifying")
    await wq.set_sender_fields(
        shop_id, status=status,
        quality_rating=sender.quality_rating,
        messaging_limit=sender.messaging_limit,
        offline_reason=sender.offline_reason,
    )
    if status == "online":
        await _finalize(shop_id, row, settings)
    return {"ok": True, "status": status}


async def _finalize(shop_id: UUID, row: dict, settings) -> None:
    """Unbind the OTP webhook and get the salon's templates submitted."""
    if row.get("source") == "kairo":
        telephony = await voice_telephony_queries.get_telephony(shop_id)
        if telephony and telephony.get("kairo_number_sid"):
            try:
                await twa.set_sms_webhook(
                    number_sid=telephony["kairo_number_sid"], sms_url="",
                    account_sid=settings.twilio_account_sid,
                    auth_token=settings.twilio_auth_token,
                )
            except Exception:  # noqa: BLE001 — a stuck webhook must not block sending
                logger.exception("whatsapp.otp_webhook_unbind_failed shop=%s", shop_id)
    await ensure_templates(shop_id=shop_id, settings=settings)


async def ensure_templates(*, shop_id: UUID, settings) -> dict:
    """Create and submit every catalogue template in the salon's own subaccount.

    Templates are per-WABA, so the same skeleton is a different ContentSid for
    every salon and needs its own Meta approval. Already-created templates are
    skipped: resubmitting is not free — Meta blocks reusing a deleted
    template's name for 30 days.
    """
    row = await wq.get_sender(shop_id)
    if not row or not row.get("subaccount_sid"):
        return {"ok": False, "error": "not_started"}

    created = 0
    for key, tpl in CATALOGUE.items():
        if await wq.get_template(shop_id, key):
            continue
        content_sid = await twa.create_template(
            subaccount_sid=row["subaccount_sid"],
            auth_token=settings.twilio_auth_token,
            friendly_name=f"kairo_{key}",
            language=tpl.language,
            body_text=tpl.body,
            sample_variables=tpl.sample,
        )
        status = await twa.submit_for_approval(
            content_sid=content_sid, name=f"kairo_{key}", category=tpl.category,
            subaccount_sid=row["subaccount_sid"],
            auth_token=settings.twilio_auth_token,
        )
        await wq.upsert_template(
            shop_id=shop_id, template_key=key, content_sid=content_sid,
            category=tpl.category, status=status, variable_count=tpl.variables,
        )
        created += 1
    return {"ok": True, "created": created}


async def sweep(*, settings) -> dict:
    """Hourly: pick up Meta's verdicts on senders and templates.

    Both are asynchronous and neither has a webhook we receive today, so
    polling is the only way a salon's status ever stops being 'in attesa'.
    One bad shop is logged and skipped, never allowed to abort the sweep.
    """
    counts = {"senders": 0, "online": 0, "templates": 0, "approved": 0, "errors": 0}

    for row in await wq.list_verifying_senders():
        try:
            sender = await twa.fetch_sender(
                sender_sid=row["sender_sid"],
                subaccount_sid=row["subaccount_sid"],
                auth_token=settings.twilio_auth_token,
            )
            status = _STATUS.get(sender.status, "verifying")
            await wq.set_sender_fields(
                row["shop_id"], status=status,
                quality_rating=sender.quality_rating,
                messaging_limit=sender.messaging_limit,
                offline_reason=sender.offline_reason,
            )
            counts["senders"] += 1
            if status == "online" and row["status"] != "online":
                await _finalize(row["shop_id"], row, settings)
                counts["online"] += 1
        except Exception:  # noqa: BLE001 — one shop must not abort the sweep
            logger.exception("whatsapp.sender_poll_failed shop=%s", row["shop_id"])
            counts["errors"] += 1

    for tpl in await wq.list_unresolved_templates():
        try:
            approval = await twa.fetch_approval(
                content_sid=tpl["content_sid"],
                subaccount_sid=tpl["subaccount_sid"],
                auth_token=settings.twilio_auth_token,
            )
            await wq.set_template_status(
                content_sid=tpl["content_sid"], status=approval.status,
                rejection_reason=approval.rejection_reason,
            )
            counts["templates"] += 1
            if approval.status == "approved":
                counts["approved"] += 1
        except Exception:  # noqa: BLE001 — see above
            logger.exception("whatsapp.template_poll_failed sid=%s", tpl["content_sid"])
            counts["errors"] += 1

    return counts


async def handle_otp_sms(*, to_number: str, body: str, settings) -> bool:
    """Meta's verification SMS landed on a Kairo-owned number. Use the code.

    Returns True if a code was found and submitted. Deliberately narrow: it
    matches a 6-digit run and nothing else, and only for a shop that is
    actually mid-verification — so a stray SMS to the salon's number can
    never re-trigger verification for a live sender.
    """
    match = re.search(r"\b(\d{6})\b", body or "")
    if not match:
        return False
    row = await wq.get_sender_by_phone(normalize_e164(to_number) or to_number)
    if not row or row.get("status") not in ("verifying", "pending_signup"):
        return False
    await submit_code(shop_id=row["shop_id"], code=match.group(1), settings=settings)
    return True
