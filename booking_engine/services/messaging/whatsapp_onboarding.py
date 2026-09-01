"""Onboard one salon onto WhatsApp: Embedded Signup -> WABA -> templates.

**One round trip, not three.** The Twilio version needed `start` (create a
subaccount), `attach_waba` (register a sender), and `submit_code` (relay Meta's
ownership OTP), plus a temporary inbound-SMS webhook to catch that OTP on a
Kairo-owned number. All of it is gone. Meta's Embedded Signup popup does the
verification itself and hands the browser back a WABA id, a phone number id
and a one-time code; `complete()` turns those into a sender that can send.

**Coexistence only — BYO WABA, nothing else.** The salon's existing WhatsApp
Business App number stays live on their phone: they keep chatting with
clients from the app while we send templates through Cloud API. This is the
path the feature exists for, and the one Twilio cannot offer at all (its
migration path requires deleting the WhatsApp Business App account on that
number). An earlier version also supported `source='new'` — a fresh WABA on a
number not yet on WhatsApp, provisioned through us rather than brought by the
salon — removed 2026-08-30: every sender is BYO WABA now, so there is no
second path to keep in sync, and `register_phone_number` (only ever called for
that path) is gone with it.

See CLAUDE.md §2026-08-24, §2026-08-30 and docs/knowledge/api/whatsapp.md.
"""
from __future__ import annotations

import logging
from uuid import UUID

from booking_engine.clients import meta_whatsapp as meta
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import meta_limits
from booking_engine.services.messaging.whatsapp_templates import (
    CATALOGUE, SUPPORTED_LANGUAGES, resolve_language,
)

logger = logging.getLogger(__name__)

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


def template_name(template_key: str, language: str) -> str:
    """The name this template carries inside every salon's WABA: `it_promo_v1`.

    **Composable, and that is the point.** Meta scopes a template name per WABA
    and cannot translate one, so a second locale is a second template with its
    own name — the platform picks between them by composing the shop's locale
    with the catalogue key, never by looking anything up. The `kairo_` prefix
    this replaced (2026-09-01) could name only one language's copy.

    Still the same string for every shop on a given locale: the catalogue is
    Kairo's, and a per-shop name would make "is promo_v1 approved for this
    salon?" unanswerable without a lookup. Uniqueness is per-WABA on Meta's
    side, and per (shop_id, template_key) on ours.

    `language` is required on purpose. Defaulting it would let a caller that
    never thought about locale silently address the Italian copy.
    """
    return f"{language}_{template_key}"


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


async def start(*, shop_id: UUID, display_name: str, settings) -> dict:
    """Record intent and hand back the Embedded Signup config.

    Nothing is created provider-side here — unlike the Twilio version, which
    had to create a subaccount before the salon had done anything, and leaked
    one on every abandoned onboarding.
    """
    existing = await wq.get_sender(shop_id)
    if existing and existing.get("status") == "online":
        return {"ok": True, "status": "online",
                "phone_number": existing["phone_number"]}

    await wq.upsert_sender(shop_id=shop_id, display_name=display_name, source="coexistence")
    await wq.set_sender_fields(shop_id, status="pending_signup")
    return {"ok": True, "status": "pending_signup", "signup": signup_config(settings)}


async def complete(
    *, shop_id: UUID, code: str, waba_id: str, phone_number_id: str, settings,
) -> dict:
    """The salon finished Meta's popup. Turn its output into a live sender.

    Order matters and is not arbitrary:

    1. **Exchange the code first.** It is single-use and short-lived; every
       later step needs the token it produces.
    2. **Subscribe to webhooks before anything else provider-side.** Without
       the subscription we receive no delivery status, no template verdicts
       and no opt-outs, while every send still succeeds — broken in the one
       way nothing would surface. No registration step follows it: a
       coexistence number is already registered, and Meta's guidance is
       explicitly not to call `/register` on one.
    3. Read the number back rather than trusting the popup, which told the
       *browser* what happened.
    4. Templates last: they are the only step that is safely re-runnable, and
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
    if not number.is_on_biz_app:
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


async def approved_on_kairo_waba(settings) -> set[tuple[str, str]]:
    """Which (language, key) pairs Meta has approved on *Kairo's own* WABA.

    Keyed by locale as well as key since 2026-09-01: `it_promo_v1` being
    approved says nothing about `en_promo_v1`, which is a separate template
    with a separate verdict on the same WABA.

    Split out of `ensure_templates` so the sweep asks Meta once per run rather
    than once per shop: the answer is the same for everyone, and it was N shops
    × M keys of identical Graph calls every hour.

    Fails closed. No `meta_kairo_waba_id`/`meta_kairo_token` configured means an
    empty set — nothing propagates — never "propagate unchecked". A Graph error
    is the same: `fetch_template` raises, the caller logs it, and the shop is
    retried next tick rather than pushed to on a guess.
    """
    if not settings.meta_kairo_waba_id or not settings.meta_kairo_token:
        return set()
    approved = set()
    for language in SUPPORTED_LANGUAGES:
        for key in CATALOGUE:
            verdict = await meta.fetch_template(
                waba_id=settings.meta_kairo_waba_id,
                name=template_name(key, language),
                token=settings.meta_kairo_token,
            )
            if verdict and verdict.status == "approved":
                approved.add((language, key))
    return approved


async def ensure_templates(
    *, shop_id: UUID, settings, approved: set[tuple[str, str]] | None = None,
) -> dict:
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
    with it).

    `approved` is the gate's answer, passed in by the sweep so it is computed
    once for the whole run; alone, this function asks for itself.
    """
    row = await wq.get_sender(shop_id)
    if not row or not row.get("waba_id") or not row.get("access_token"):
        return {"ok": False, "error": "not_started"}

    if approved is None:
        approved = await approved_on_kairo_waba(settings)

    # The salon's own locale decides which copy it gets, read live rather than
    # snapshotted onto the sender — see `get_shop_language`.
    language = resolve_language(await wq.get_shop_language(shop_id))

    created, failed, not_ready = 0, [], []
    for key, tpl in CATALOGUE.items():
        if await wq.get_template(shop_id, key):
            continue
        name = template_name(key, language)

        if (language, key) not in approved:
            not_ready.append(key)
            continue

        try:
            meta_id, status = await meta.create_template(
                waba_id=row["waba_id"], token=row["access_token"],
                name=name, language=language, category=tpl.category,
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
            meta_template_id=meta_id, language=language,
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

    It also carries the **only** retry of the propagation gate. A salon that
    onboards while Kairo's copy of a template is still pending gets nothing,
    and the approval that unblocks it lands later, on Kairo's WABA, with no
    per-shop event attached — so if this loop didn't go back for them, nobody
    would.
    """
    counts = {"senders": 0, "online": 0, "templates": 0, "approved": 0,
              "propagated": 0, "errors": 0}

    # Once per sweep, not once per shop: same question, same answer for all.
    try:
        approved = await approved_on_kairo_waba(settings)
    except Exception:  # noqa: BLE001 — a Graph blip must not skip the rest
        logger.exception("whatsapp.kairo_gate_failed")
        approved = set()
    counts["approved_on_kairo"] = len(approved)

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
            await ensure_templates(
                shop_id=row["shop_id"], settings=settings, approved=approved
            )
            counts["online"] += 1
        except Exception:  # noqa: BLE001 — one shop must not abort the sweep
            logger.exception("whatsapp.sender_poll_failed shop=%s", row["shop_id"])
            counts["errors"] += 1

    # Live senders still missing part of the catalogue. Skipped entirely when
    # the gate is empty — there is nothing to give them, and asking would be
    # one pointless query per shop per hour.
    if approved:
        for row in await wq.list_senders_missing_templates(len(CATALOGUE)):
            try:
                result = await ensure_templates(
                    shop_id=row["shop_id"], settings=settings, approved=approved
                )
                if result.get("created"):
                    counts["propagated"] += result["created"]
                    logger.info(
                        "whatsapp.templates_propagated_late shop=%s created=%s",
                        row["shop_id"], result["created"],
                    )
            except Exception:  # noqa: BLE001 — see above
                logger.exception("whatsapp.late_propagation_failed shop=%s",
                                 row["shop_id"])
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


async def retire_template(*, template_key: str, settings) -> dict:
    """Delete one template from Kairo's WABA and from every customer's.

    **Deliberately not part of the sweep.** The sweep could infer "gone from
    Kairo's WABA → delete downstream", but then one transient Graph read error
    reads as a deletion and wipes the template from every customer's WABA at
    once. A destructive fan-out gets an explicit operator behind it:
    `scripts/kairo_waba.py retire-template --key promo_v1`.

    **Kairo's copy goes first, and that order is the whole design.** The
    reverse — customers first — leaves the gate still answering "approved" if
    the last step fails, and the next sweep cheerfully re-pushes everything
    just deleted. Deleting ours first closes the gate, so a partial run stops
    dead and re-running finishes it.

    "Already gone" counts as success at every step: Meta 404s a name it doesn't
    have, and a partial retry must be able to complete.
    """
    if template_key not in CATALOGUE:
        return {"ok": False, "error": "unknown_template"}
    if not settings.meta_kairo_waba_id or not settings.meta_kairo_token:
        return {"ok": False, "error": "kairo_waba_not_configured"}

    # Every locale's copy of the key: they are separate templates on the same
    # WABA, and leaving one behind leaves the propagation gate answering
    # "approved" for the shops running that locale.
    for language in SUPPORTED_LANGUAGES:
        name = template_name(template_key, language)
        try:
            await meta.delete_template(
                waba_id=settings.meta_kairo_waba_id, name=name,
                token=settings.meta_kairo_token,
            )
        except meta.MetaError as exc:
            # 2593002 / 100 — no such template. Ours is already gone, which is
            # the state we wanted; carry on to the customers who still have it.
            logger.warning("whatsapp.kairo_template_delete_failed name=%s err=%s",
                           name, exc)

    deleted, failed = 0, []
    for row in await wq.list_senders_with_template(template_key):
        try:
            await meta.delete_template(
                waba_id=row["waba_id"], name=row["name"], token=row["access_token"]
            )
        except meta.MetaError as exc:
            # The row is dropped anyway when Meta says the template isn't
            # there; anything else is a real failure and keeps the row so the
            # next run retries it.
            logger.warning("whatsapp.template_delete_failed shop=%s err=%s",
                           row["shop_id"], exc)
            failed.append(str(row["shop_id"]))
            continue
        await wq.delete_template_row(
            shop_id=row["shop_id"], template_key=template_key
        )
        deleted += 1

    logger.info("whatsapp.template_retired key=%s deleted=%s failed=%s",
                template_key, deleted, len(failed))
    return {"ok": True, "template_key": template_key,
            "deleted": deleted, "failed": failed}
