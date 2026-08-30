"""Onboard one salon onto WhatsApp: Embedded Signup -> WABA -> templates.

**One round trip, not three.** The Twilio version needed `start` (create a
subaccount), `attach_waba` (register a sender), and `submit_code` (relay Meta's
ownership OTP), plus a temporary inbound-SMS webhook to catch that OTP on a
Kairo-owned number. All of it is gone. Meta's Embedded Signup popup does the
verification itself and hands the browser back a WABA id, a phone number id
and a one-time code; `complete()` turns those into a sender that can send.

The two paths, both branches inside Meta's own popup rather than two
integrations:

- `coexistence` — the salon's existing WhatsApp Business App number. It stays
  live on their phone: they keep chatting with clients from the app while we
  send templates through Cloud API. This is the path the feature exists for,
  and the one Twilio cannot offer at all (its migration path requires deleting
  the WhatsApp Business App account on that number).
- `new` — a fresh WABA on a number not yet on WhatsApp. The only path that
  calls `register_phone_number`; a coexistence number is already registered
  and Meta's guidance is explicitly not to call it.

See CLAUDE.md §2026-08-24 and docs/knowledge/api/whatsapp.md.
"""
from __future__ import annotations

import logging
from uuid import UUID

from booking_engine.clients import meta_whatsapp as meta
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import meta_limits
from booking_engine.services.messaging.whatsapp_templates import CATALOGUE

logger = logging.getLogger(__name__)

SOURCES = ("coexistence", "new")

# Meta's template statuses -> ours. Anything unrecognised stays 'pending' so a
# new Meta state can never silently mark a template sendable.
TEMPLATE_STATUS = {
    "approved": "approved",
    "rejected": "rejected",
    "paused": "paused",
    "disabled": "disabled",
    "pending": "pending",
    "in_appeal": "pending",
    "pending_deletion": "disabled",
}


def template_name(template_key: str) -> str:
    """The name this template carries inside every salon's WABA.

    Deliberately the same string for every shop: the catalogue is Kairo's, and
    a per-shop name would make "is promo_v1 approved for this salon?"
    unanswerable without a lookup. Uniqueness is per-WABA on Meta's side, and
    per (shop_id, template_key) on ours.
    """
    return f"kairo_{template_key}"


def signup_config(settings) -> dict:
    """What the webapp needs to open Meta's popup, from one source of truth."""
    return {
        "app_id": settings.meta_app_id,
        "config_id": settings.meta_config_id,
        "solution_id": settings.meta_solution_id,
        # Turns the popup's first question into "connect your existing
        # WhatsApp Business App account?" — the entire point of this design.
        "feature_type": "whatsapp_business_app_onboarding",
        "session_info_version": "3",
    }


async def start(*, shop_id: UUID, display_name: str, source: str, settings) -> dict:
    """Record intent and hand back the Embedded Signup config.

    Nothing is created provider-side here — unlike the Twilio version, which
    had to create a subaccount before the salon had done anything, and leaked
    one on every abandoned onboarding.
    """
    if source not in SOURCES:
        return {"ok": False, "error": "invalid_source"}

    existing = await wq.get_sender(shop_id)
    if existing and existing.get("status") == "online":
        return {"ok": True, "status": "online",
                "phone_number": existing["phone_number"]}

    await wq.upsert_sender(shop_id=shop_id, display_name=display_name, source=source)
    await wq.set_sender_fields(shop_id, status="pending_signup")
    return {"ok": True, "status": "pending_signup", "signup": signup_config(settings)}


async def complete(
    *, shop_id: UUID, code: str, waba_id: str, phone_number_id: str,
    pin: str | None, settings,
) -> dict:
    """The salon finished Meta's popup. Turn its output into a live sender.

    Order matters and is not arbitrary:

    1. **Exchange the code first.** It is single-use and short-lived; every
       later step needs the token it produces.
    2. **Subscribe to webhooks before anything else provider-side.** Without
       the subscription we receive no delivery status, no template verdicts
       and no opt-outs, while every send still succeeds — broken in the one
       way nothing would surface.
    3. Register the number, but only for `source='new'`.
    4. Read the number back rather than trusting the popup, which told the
       *browser* what happened.
    5. Templates last: they are the only step that is safely re-runnable, and
       `ensure_templates` is exposed separately for exactly that reason.
    """
    row = await wq.get_sender(shop_id)
    if not row:
        return {"ok": False, "error": "not_started"}
    if row.get("status") == "online":
        return {"ok": True, "status": "online"}

    # Meta's Tech Provider onboarding cap, checked before we spend the popup's
    # single-use code. Exceeding it fails at Meta with an opaque error and
    # leaves the salon staring at a broken flow they can't retry — refusing
    # here at least says which limit was hit and that waiting fixes it.
    limit = meta_limits.onboarding_limit(getattr(settings, "meta_access_verified", False))
    recent = await wq.onboarded_last_7_days()
    if recent >= limit:
        logger.warning("whatsapp.onboarding_limit_reached recent=%s limit=%s",
                       recent, limit)
        return {"ok": False, "error": "onboarding_limit_reached",
                "onboarded_last_7_days": recent, "limit": limit}

    try:
        token = await meta.exchange_code(
            code=code, app_id=settings.meta_app_id,
            app_secret=settings.meta_app_secret,
        )
    except meta.MetaError as exc:
        logger.warning("whatsapp.code_exchange_failed shop=%s err=%s", shop_id, exc)
        return {"ok": False, "error": "code_exchange_failed"}

    # Written before the calls that use it: a crash after this point leaves a
    # resumable row, where losing the token would leave a WABA we can neither
    # reach nor unsubscribe from.
    await wq.set_sender_fields(
        shop_id, access_token=token, waba_id=waba_id,
        phone_number_id=phone_number_id,
    )

    try:
        await meta.subscribe_app(waba_id=waba_id, token=token)

        if row["source"] == "new":
            if not pin:
                return {"ok": False, "error": "pin_required"}
            await meta.register_phone_number(
                phone_number_id=phone_number_id, pin=pin, token=token
            )

        number = await meta.get_phone_number(
            phone_number_id=phone_number_id, token=token
        )
    except meta.MetaError as exc:
        await wq.set_sender_fields(
            shop_id, status="failed", offline_reason=str(exc)[:500]
        )
        return {"ok": False, "error": "meta_error", "detail": str(exc)}

    # A coexistence onboarding that didn't actually land on the Business App
    # is worth knowing about: the salon believes they kept their app.
    if row["source"] == "coexistence" and not number.is_on_biz_app:
        logger.warning(
            "whatsapp.coexistence_not_confirmed shop=%s platform=%s",
            shop_id, number.platform_type,
        )

    await wq.set_sender_fields(
        shop_id,
        status="online",
        phone_number=number.display_phone_number or row.get("phone_number"),
        display_name=number.verified_name or row["display_name"],
        quality_rating=number.quality_rating,
        messaging_limit=number.messaging_limit,
        throughput_level=number.throughput_level,
        platform_type=number.platform_type,
        offline_reason=None,
    )

    templates = await ensure_templates(shop_id=shop_id, settings=settings)
    return {"ok": True, "status": "online",
            "phone_number": number.display_phone_number,
            "coexistence": number.is_on_biz_app,
            "templates": templates.get("created", 0)}


async def ensure_templates(*, shop_id: UUID, settings) -> dict:
    """Inject Kairo's catalogue into the salon's own WABA.

    This is the call Twilio structurally could not make — a WABA it doesn't
    own is closed to it — and the reason the whole channel moved to Meta
    direct. Already-created templates are skipped: Meta blocks reusing a
    deleted template's name for 30 days, so resubmitting is not free.

    **Gated on Kairo's own copy being approved first.** A template is created
    by hand on Kairo's WABA (`scripts/kairo_waba.py push-templates`) and
    reviewed there before this function will push it to any salon — a
    rejection is a Meta judgment on the *content*, identical whatever WABA it's
    submitted to, so testing on one WABA before N customer WABAs avoids
    burning the same rejection N times (and the quality-rating hit that comes
    with it). Fails closed: no `meta_kairo_waba_id`/`meta_kairo_token`
    configured means nothing propagates, not "propagate unchecked."
    """
    row = await wq.get_sender(shop_id)
    if not row or not row.get("waba_id") or not row.get("access_token"):
        return {"ok": False, "error": "not_started"}

    created, failed, not_ready = 0, [], []
    for key, tpl in CATALOGUE.items():
        if await wq.get_template(shop_id, key):
            continue
        name = template_name(key)

        if not settings.meta_kairo_waba_id or not settings.meta_kairo_token:
            not_ready.append(key)
            continue
        kairo_verdict = await meta.fetch_template(
            waba_id=settings.meta_kairo_waba_id, name=name,
            token=settings.meta_kairo_token,
        )
        if not kairo_verdict or kairo_verdict.status != "approved":
            not_ready.append(key)
            continue

        try:
            meta_id, status = await meta.create_template(
                waba_id=row["waba_id"], token=row["access_token"],
                name=name, language=tpl.language, category=tpl.category,
                body_text=tpl.body, sample_variables=tpl.sample,
            )
        except meta.MetaError as exc:
            # One rejected template must not stop the rest of the catalogue.
            logger.warning("whatsapp.template_create_failed shop=%s key=%s err=%s",
                           shop_id, key, exc)
            failed.append(key)
            continue
        await wq.upsert_template(
            shop_id=shop_id, template_key=key, name=name,
            meta_template_id=meta_id, language=tpl.language,
            category=tpl.category, status=TEMPLATE_STATUS.get(status, "pending"),
            variable_count=tpl.variables,
        )
        created += 1
    return {"ok": True, "created": created, "failed": failed, "not_ready": not_ready}


async def sweep(*, settings) -> dict:
    """Hourly reconciler for what the webhooks should already have told us.

    Template verdicts arrive as `message_template_status_update` webhooks
    within minutes. This exists because a *missed* webhook leaves a template
    `pending` forever, which blocks every send for that shop and looks like
    nothing at all. One bad shop is logged and skipped, never allowed to abort
    the sweep.
    """
    counts = {"senders": 0, "online": 0, "templates": 0, "approved": 0, "errors": 0}

    for row in await wq.list_verifying_senders():
        try:
            number = await meta.get_phone_number(
                phone_number_id=row["phone_number_id"], token=row["access_token"]
            )
            counts["senders"] += 1
            # A number Meta can describe is a number Meta has accepted.
            await wq.set_sender_fields(
                row["shop_id"], status="online",
                phone_number=number.display_phone_number,
                quality_rating=number.quality_rating,
                messaging_limit=number.messaging_limit,
                throughput_level=number.throughput_level,
                platform_type=number.platform_type,
            )
            await ensure_templates(shop_id=row["shop_id"], settings=settings)
            counts["online"] += 1
        except Exception:  # noqa: BLE001 — one shop must not abort the sweep
            logger.exception("whatsapp.sender_poll_failed shop=%s", row["shop_id"])
            counts["errors"] += 1

    for tpl in await wq.list_unresolved_templates():
        try:
            verdict = await meta.fetch_template(
                waba_id=tpl["waba_id"], name=tpl["name"], token=tpl["access_token"]
            )
            if verdict is None:
                continue
            status = TEMPLATE_STATUS.get(verdict.status, "pending")
            await wq.set_template_status(
                shop_id=tpl["shop_id"], name=tpl["name"], status=status,
                rejection_reason=verdict.rejection_reason,
            )
            counts["templates"] += 1
            if status == "approved":
                counts["approved"] += 1
        except Exception:  # noqa: BLE001 — see above
            logger.exception("whatsapp.template_poll_failed shop=%s name=%s",
                             tpl["shop_id"], tpl["name"])
            counts["errors"] += 1

    return counts
