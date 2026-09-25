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

See AGENTS.md §2026-08-24, §2026-08-30 and docs/knowledge/api/whatsapp.md.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from booking_engine.clients import meta_whatsapp as meta
from booking_engine.clients import webapp_notify
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import meta_limits
from booking_engine.services.messaging.whatsapp_templates import (
    CATALOGUE, DOCUMENT_TEMPLATES, SUPPORTED_LANGUAGES, body_hash,
    propagation_fingerprints, resolve_language,
)

logger = logging.getLogger(__name__)

# How early to start asking the owner to reconnect, and how rarely to repeat
# it. Seven days matches the webapp banner's own window (`RENEW_WINDOW_DAYS`
# in lib/whatsapp/client.ts) — two different answers to "is it urgent yet?"
# would be the kind of drift nobody notices until a salon goes dark. Three
# days between mails means at most three across the window: enough to catch a
# salon closed for a long weekend, few enough not to become noise.
RENEW_WINDOW_DAYS = 7
REMINDER_COOLDOWN_HOURS = 72

# Longer than any real popup interaction; a pending row this old was abandoned.
ABANDONED_AFTER = timedelta(minutes=15)


def is_abandoned(sender: dict, *, now: datetime | None = None) -> bool:
    """A `pending_signup` row untouched for a while is an abandoned popup.

    The webapp aborts explicitly when its popup closes, but an owner can walk
    away from (or close) the whole tab and nothing fires. Interpreting such a
    row as never-started at read time is what lets the panel offer the connect
    button again instead of saying "Meta is verifying" forever.
    """
    if sender.get("status") != "pending_signup":
        return False
    updated = sender.get("updated_at")
    if not updated:
        return False
    return (now or datetime.now(timezone.utc)) - updated > ABANDONED_AFTER

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


async def start(
    *, shop_id: UUID, display_name: str, settings, reconnect: bool = False,
) -> dict:
    """Record intent and hand back the Embedded Signup config.

    Nothing is created provider-side here — unlike the Twilio version, which
    had to create a subaccount before the salon had done anything, and leaked
    one on every abandoned onboarding.

    `reconnect` is the token-refresh path: our Login Configuration mints
    60-day tokens and nothing renews them, so the only way to get a fresh one
    is the salon redoing the popup. It returns the config **without touching
    the row** — the sender stays `online` on its old token, still sending,
    right up until `complete(reconnect=True)` swaps in the new one. Marking it
    `pending_signup` here would take a working sender off the air for however
    long the owner leaves the popup open, to fix a problem that hasn't
    happened yet.
    """
    existing = await wq.get_sender(shop_id)
    if existing and existing.get("status") == "online" and not reconnect:
        return {"ok": True, "status": "online",
                "phone_number": existing["phone_number"]}
    if reconnect:
        if not existing:
            return {"ok": False, "error": "not_started"}
        return {"ok": True, "status": existing["status"],
                "signup": signup_config(settings)}

    await wq.upsert_sender(shop_id=shop_id, display_name=display_name, source="coexistence")
    await wq.set_sender_fields(shop_id, status="pending_signup")
    return {"ok": True, "status": "pending_signup", "signup": signup_config(settings)}


async def _named(waba_ids: list[str], token: str) -> list[dict]:
    """Label the candidates so the owner picks something they recognise.

    Name *and* phone numbers, because the name alone often isn't enough: a
    WABA is frequently named after a company registration the owner has never
    read, and two of them side by side say nothing about which is the salon's.
    The number is the thing they know by heart.

    Best effort on both: a label that won't load is not a reason to refuse an
    onboarding, so it degrades to the id rather than raising.
    """
    out = []
    for wid in waba_ids:
        try:
            name = await meta.get_waba_name(waba_id=wid, token=token)
        except meta.MetaError:
            name = wid
        try:
            numbers = await meta.list_phone_numbers(waba_id=wid, token=token)
        except meta.MetaError:
            numbers = []
        out.append({"id": wid, "name": name, "phone_numbers": numbers})
    return out


async def complete(
    *, shop_id: UUID, code: str | None = None, settings,
    waba_id: str | None = None, phone_number_id: str | None = None,
    redirect_uri: str | None = None, reconnect: bool = False,
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
    4. Templates are **not** pushed here — see the note where this returns.
       They are the only safely re-runnable step, which is what lets them move
       off the request path without a gap opening.

    **Called a second time without a `code` to resolve an ambiguity.** When the
    owner administers more than one WABA the first call cannot know which one
    they meant, so it stops and asks. The `code` is single-use and by then
    spent, which is why this resumes from the token persisted below rather than
    re-exchanging it — redoing the popup to answer a question about the popup's
    own result is the shape to avoid.
    """
    row = await wq.get_sender(shop_id)
    if not row:
        return {"ok": False, "error": "not_started"}
    # The early return is what makes a double-submit idempotent. A reconnect
    # is the one case where an online sender legitimately runs this again.
    if row.get("status") == "online" and not reconnect:
        return {"ok": True, "status": "online"}

    # Meta's Tech Provider onboarding cap, checked before we spend the popup's
    # single-use code. Exceeding it fails at Meta with an opaque error and
    # leaves the salon staring at a broken flow they can't retry — refusing
    # here at least says which limit was hit and that waiting fixes it.
    #
    # Skipped on a reconnect: the cap counts *new customers per rolling 7
    # days*, and a salon refreshing its own token is not one. Counting it
    # would mean a busy onboarding week silently blocks an existing salon from
    # renewing — the sender then dies at day 60 because of someone else's
    # signup.
    # Skipped on a resume too (no code): the popup already happened and was
    # already counted. Re-checking would let a busy onboarding week strand a
    # salon halfway through, holding a token and unable to name its WABA.
    if code and not reconnect:
        limit = meta_limits.onboarding_limit(
            getattr(settings, "meta_access_verified", False)
        )
        recent = await wq.onboarded_last_7_days()
        if recent >= limit:
            logger.warning("whatsapp.onboarding_limit_reached recent=%s limit=%s",
                           recent, limit)
            return {"ok": False, "error": "onboarding_limit_reached",
                    "onboarded_last_7_days": recent, "limit": limit}

    if not code:
        # Resuming to answer an ambiguity. The token below is the one this
        # function persisted on the call that asked the question.
        token = row.get("access_token")
        if not token:
            return {"ok": False, "error": "not_started"}
        expires_in = None
    else:
        try:
            token, expires_in = await meta.exchange_code(
                code=code, app_id=settings.meta_app_id,
                app_secret=settings.meta_app_secret,
                redirect_uri=redirect_uri,
            )
        except meta.MetaError as exc:
            logger.warning("whatsapp.code_exchange_failed shop=%s err=%s", shop_id, exc)
            return {"ok": False, "error": "code_exchange_failed"}

    # NULL means Meta reported no expiry. Nothing renews an expiring token:
    # a business token is minted by the salon completing Embedded Signup, so
    # the only recovery is asking them to reconnect. Recording the date is
    # what makes that visible before every send starts failing.
    # The nudge that gets it renewed is the webapp's: `GET /whatsapp/status`
    # returns this date, and the cockpit banner + panel ask the owner to
    # reconnect inside the last 7 days (`start`/`complete` with
    # `reconnect=True`).
    # ponytail: pull-only — the owner has to open the app to see it. A push
    # notification from the tick is the upgrade if a salon ever expires
    # anyway.
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if expires_in else None
    )
    if expires_in:
        logger.warning(
            "whatsapp.token_expires shop=%s at=%s — sender goes silent unless "
            "the salon reconnects", shop_id, expires_at,
        )

    # Persisted here, before the first call that uses it. A crash past this
    # point leaves a resumable row; losing the token would leave a WABA we can
    # neither reach nor unsubscribe from — and the code that produced it is
    # spent, so it cannot be minted again without another popup. This is also
    # what makes the ambiguity answerable below without a second signup.
    if code:
        await wq.set_sender_fields(
            shop_id, access_token=token, token_expires_at=expires_at,
        )

    # The popup's ids are optional, and normally absent. Meta only posts
    # `WA_EMBEDDED_SIGNUP` — the message carrying waba_id and phone_number_id —
    # when the flow runs through its JS SDK, and ours cannot: the SDK routes
    # `FB.login` through FedCM and drops `config_id`, so the popup that opens
    # is a plain OIDC login Meta then refuses. The browser therefore has only
    # the code, and the ids are read back from the token here.
    #
    # Better this way round regardless. The old version trusted two strings the
    # browser had been handed; this asks Meta what it actually granted. They
    # are still accepted when supplied, so an SDK-based caller keeps working.
    if not waba_id:
        try:
            granted = await meta.waba_ids_for_token(
                token=token, app_id=settings.meta_app_id,
                app_secret=settings.meta_app_secret,
            )
        except meta.MetaError as exc:
            logger.warning("whatsapp.waba_lookup_failed shop=%s err=%s", shop_id, exc)
            return {"ok": False, "error": "waba_lookup_failed"}
        # Neither zero nor several can be resolved by guessing: zero means the
        # grant did not include a WABA, several means the owner administers
        # more than one and only they know which is the salon's. Guessing
        # attaches the sender to someone else's WhatsApp account.
        if len(granted) != 1:
            logger.warning("whatsapp.waba_ambiguous shop=%s ids=%s", shop_id, granted)
            return {"ok": False, "error": "waba_ambiguous",
                    "waba_ids": granted,
                    "wabas": await _named(granted, token)}
        waba_id = granted[0]

    if not phone_number_id:
        try:
            numbers = await meta.list_phone_number_ids(waba_id=waba_id, token=token)
        except meta.MetaError as exc:
            logger.warning("whatsapp.phone_lookup_failed shop=%s err=%s", shop_id, exc)
            return {"ok": False, "error": "phone_lookup_failed"}
        if len(numbers) != 1:
            logger.warning("whatsapp.phone_ambiguous shop=%s ids=%s", shop_id, numbers)
            return {"ok": False, "error": "phone_ambiguous", "phone_number_ids": numbers}
        phone_number_id = numbers[0]

    await wq.set_sender_fields(
        shop_id, waba_id=waba_id, phone_number_id=phone_number_id,
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

    # Inline, not deferred: the 24h clock starts now and a background task
    # dying silently would cost the salon its connection. The sweep retries.
    if number.is_on_biz_app:
        await sync_coexistence(await wq.get_sender(shop_id))

    # Templates are deliberately NOT pushed here. They are one Graph round trip
    # per catalogue entry against a WABA that has just been created, which took
    # the whole call past the gateway timeout in front of the webapp — the
    # sender was online and the owner saw a 504. The caller schedules the push
    # after responding, and the hourly sweep's `list_senders_needing_templates`
    # is the reconciler if that push dies: this stage was always the
    # re-runnable one, which is why it was last.
    return {"ok": True, "status": "online",
            "phone_number": number.display_phone_number,
            "coexistence": number.is_on_biz_app}


async def abort(*, shop_id: UUID) -> dict:
    """The owner closed Meta's popup without finishing, or the exchange failed.

    The other end of `start()`'s contract: it wrote a `pending_signup` row to
    record intent, and this drops it so the next status read is `not_started`
    and the panel offers the button again. Idempotent — no row, or a row past
    the pending stage, deletes nothing and still reports ok.
    """
    await wq.delete_pending_sender(shop_id)
    return {"ok": True}


async def sync_coexistence(row: dict) -> bool:
    """Request Meta's one-shot contacts-then-history sync for a sender.

    Not optional: Meta offboards a coexistence number unless this is called
    within 24h of onboarding, even if we never read the data (confirmed by
    Meta developer support, 2026-09-25). Each step is recorded as it succeeds,
    so a retry skips what already went through — both are once-only. Returns
    whether both are done; failures are logged and left to the hourly sweep.
    """
    shop_id = row["shop_id"]
    for sync_type, column in (("smb_app_state_sync", "contacts_sync_at"),
                              ("history", "history_sync_at")):
        if row.get(column):
            continue
        try:
            await meta.request_smb_sync(phone_number_id=row["phone_number_id"],
                                        token=row["access_token"], sync_type=sync_type)
        except meta.MetaError as exc:
            logger.warning("whatsapp.coexistence_sync_failed shop=%s type=%s err=%s",
                           shop_id, sync_type, exc)
            return False
        await wq.set_sender_fields(shop_id, **{column: datetime.now(timezone.utc)})
    return True


async def disconnect(*, shop_id: UUID) -> dict:
    """The owner detaches their WABA from Kairo, whatever state the sender is in.

    Unsubscribing is best effort: a token that has expired or been revoked is
    a common reason to disconnect in the first place, and failing here would
    leave the owner unable to remove a sender that already cannot send.
    Idempotent — no row deletes nothing and still reports ok.
    """
    row = await wq.get_sender(shop_id)
    if not row:
        return {"ok": True, "cancelled": 0}
    if row.get("waba_id") and row.get("access_token"):
        try:
            await meta.unsubscribe_app(waba_id=row["waba_id"], token=row["access_token"])
        except meta.MetaError as exc:
            logger.warning("whatsapp.unsubscribe_failed shop=%s err=%s", shop_id, exc)
    cancelled = await wq.delete_sender(shop_id)
    return {"ok": True, "cancelled": cancelled}


async def approved_on_kairo_waba(settings) -> set[tuple[str, str]]:
    """Which (language, key) pairs Meta has approved on *Kairo's own* WABA,
    **with the body this file currently holds**.

    Keyed by locale as well as key since 2026-09-01: `it_promo_v1` being
    approved says nothing about `en_promo_v1`, which is a separate template
    with a separate verdict on the same WABA.

    The body is compared, not just the status, because a status answers a
    question about a *name*. Change the copy here and deploy before
    `push-templates` runs, and a name-only gate would report "approved" for the
    body Meta reviewed *last month* — and the drift path below would then push
    the new, unreviewed copy straight to every customer WABA, which is exactly
    the vetting rule this gate exists to enforce.

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
        for key, tpl in CATALOGUE.items():
            verdict = await meta.fetch_template(
                waba_id=settings.meta_kairo_waba_id,
                name=template_name(key, language),
                token=settings.meta_kairo_token,
            )
            if not verdict or verdict.status != "approved":
                continue
            if verdict.body != tpl.body:
                logger.info(
                    "whatsapp.kairo_body_drift name=%s — approved copy is not the "
                    "catalogue's; run kairo_waba.py push-templates",
                    template_name(key, language),
                )
                continue
            approved.add((language, key))
    # The document templates ride the same gate. The name is Meta's preset used
    # verbatim (never locale-prefixed), and the body is the compared payload —
    # the document header comes back as an opaque `header_handle`, so status
    # plus BODY text is the whole check. Same fail-closed rule: not approved,
    # or approved with different copy, means the receipt propagates nowhere.
    for key, doc in DOCUMENT_TEMPLATES.items():
        try:
            verdict = await meta.fetch_template(
                waba_id=settings.meta_kairo_waba_id,
                name=doc.name,
                token=settings.meta_kairo_token,
            )
        except meta.MetaError as exc:
            logger.warning("whatsapp.kairo_gate_fetch_failed name=%s err=%s", doc.name, exc)
            continue
        if not verdict or verdict.status != "approved":
            continue
        if verdict.body != doc.body:
            logger.info(
                "whatsapp.kairo_body_drift name=%s — approved copy is not the "
                "catalogue's; run kairo_waba.py push-templates",
                doc.name,
            )
            continue
        approved.add((doc.language, key))
    return approved


async def ensure_templates(
    *, shop_id: UUID, settings, approved: set[tuple[str, str]] | None = None,
) -> dict:
    """Inject Kairo's catalogue into the salon's own WABA, and keep it current.

    This is the call Twilio structurally could not make — a WABA it doesn't
    own is closed to it — and the reason the whole channel moved to Meta
    direct.

    **Three outcomes per key: create, edit, or nothing.** A key already on the
    WABA with the catalogue's current body is left alone (resubmitting is not
    free — Meta blocks reusing a deleted name for 30 days). One whose body has
    since changed here is *edited in place*, keeping its name: before this, an
    existing row meant an unconditional skip, so re-voicing a template changed
    nothing for any connected salon while `status` still read `approved`.

    **Gated on Kairo's own copy being approved first.** A template is created
    by hand on Kairo's WABA (`scripts/kairo_waba.py push-templates`) and
    reviewed there before this function will push it to any salon — a
    rejection is a Meta judgment on the *content*, identical whatever WABA it's
    submitted to, so testing on one WABA before N customer WABAs avoids
    burning the same rejection N times (and the quality-rating hit that comes
    with it).

    `approved` is the gate's answer, passed in by the sweep so it is computed
    once for the whole run; alone, this function asks for itself.

    The catalogue is not the only push list (2026-09-23): `DOCUMENT_TEMPLATES`
    — the receipt — reconciles in the same run with the identical gate, skip
    and adopt rules, and a document-specific create/edit. The gate now also
    includes it, so once Kairo's own copy is approved the hourly sweep pushes
    it to every connected salon proactively; the lazy `ensure_receipt_template`
    at send time stays as the self-heal for a shop the sweep missed.
    """
    row = await wq.get_sender(shop_id)
    if not row or not row.get("waba_id") or not row.get("access_token"):
        return {"ok": False, "error": "not_started"}

    if approved is None:
        approved = await approved_on_kairo_waba(settings)

    # The salon's own locale decides which copy it gets, read live rather than
    # snapshotted onto the sender — see `get_shop_language`.
    language = resolve_language(await wq.get_shop_language(shop_id))

    created, edited, failed, not_ready = 0, 0, [], []
    for key, tpl in CATALOGUE.items():
        existing = await wq.get_template(shop_id, key)
        wanted = body_hash(tpl.body)
        if existing and existing.get("body_hash") == wanted:
            continue
        # Never touch a template Meta is still ruling on. Meta refuses to edit
        # one under review, so an hourly sweep would fail against it every hour
        # until the verdict lands — and an edit that *did* land would restart
        # the review, pushing approval further out exactly as often as we
        # asked. The drift is not lost: this shop keeps matching
        # `list_senders_needing_templates`, and the edit happens on the first
        # sweep after Meta has ruled.
        if existing and existing.get("status") in ("pending", "received"):
            continue
        name = template_name(key, language)

        if (language, key) not in approved:
            not_ready.append(key)
            continue

        try:
            if existing:
                # Same name, new body. The salon keeps sending the previously
                # approved copy while Meta re-reviews this one.
                meta_id = existing["meta_template_id"]
                status = await meta.edit_template(
                    template_id=meta_id, token=row["access_token"],
                    body_text=tpl.body, sample_variables=tpl.sample,
                )
            else:
                meta_id, status = await meta.create_template(
                    waba_id=row["waba_id"], token=row["access_token"],
                    name=name, language=language, category=tpl.category,
                    body_text=tpl.body, sample_variables=tpl.sample,
                )
        except meta.MetaError as exc:
            # The name may already exist on that WABA with no row here: a
            # create that reached Meta but whose row was lost, a re-onboarding
            # of the same WABA, or — the one that actually bit (2026-09-20) —
            # Meta having re-categorised the template on review, which makes
            # every later create refuse the category we keep sending.
            #
            # Adopt it instead of failing. Refusing meant the sweep retried the
            # identical create every hour forever while the panel showed the
            # feature as waiting for Meta, which was true of nothing.
            adopted = None
            if not existing:
                adopted = await meta.fetch_template(
                    waba_id=row["waba_id"], name=name, token=row["access_token"],
                )
            if not adopted or not adopted.id:
                # One rejected template must not stop the rest of the catalogue.
                logger.warning("whatsapp.template_push_failed shop=%s key=%s err=%s",
                               shop_id, key, exc)
                failed.append(key)
                continue
            logger.warning("whatsapp.template_adopted shop=%s key=%s status=%s "
                           "category=%s (create refused: %s)",
                           shop_id, key, adopted.status, adopted.category, exc)
            meta_id, status = adopted.id, adopted.status
            # Meta's body, not the catalogue's, so a template we adopted with
            # stale copy reads as drifted and the edit path fixes it next run
            # rather than the row claiming an alignment nobody checked.
            wanted = body_hash(adopted.body)
            # Meta's category too: storing our guess is what makes the next
            # create repeat the same refusal.
            tpl_category = adopted.category or tpl.category
        else:
            tpl_category = tpl.category
        await wq.upsert_template(
            shop_id=shop_id, template_key=key, name=name,
            meta_template_id=meta_id, language=language,
            category=tpl_category, status=TEMPLATE_STATUS.get(status, "pending"),
            variable_count=tpl.variables, body_hash=wanted,
        )
        if existing:
            edited += 1
        else:
            created += 1

    # The document templates (the receipt) ride the same reconcile, with the
    # document-specific create: a HEADER/DOCUMENT component that needs
    # `META_RECEIPT_SAMPLE_URL` (a sample PDF for Meta to review) and Meta's
    # preset name used verbatim. Every gate/skip/adopt rule is the catalogue's.
    for key, doc in DOCUMENT_TEMPLATES.items():
        existing = await wq.get_template(shop_id, key)
        wanted = body_hash(doc.body)
        if existing and existing.get("body_hash") == wanted:
            continue
        # Same rule as the catalogue: Meta refuses to edit a template it is
        # still ruling on, so the drift waits for the first sweep after the
        # verdict — the shop keeps matching `list_senders_needing_templates`.
        if existing and existing.get("status") in ("pending", "received"):
            continue
        if (doc.language, key) not in approved:
            not_ready.append(key)
            continue
        # The sample URL is only needed to CREATE (an edit resubmits just the
        # body, and the header is untouched). Submitting without it would be a
        # guaranteed Meta rejection, so it is reported as not_ready instead —
        # one missing setting must not abort the results above.
        if not existing and not settings.meta_receipt_sample_url:
            logger.warning(
                "whatsapp.receipt_sample_not_configured shop=%s key=%s — "
                "META_RECEIPT_SAMPLE_URL must be set to create the document "
                "template", shop_id, key,
            )
            not_ready.append(key)
            continue
        try:
            if existing:
                # Body-only edit: Meta never hands back the header_handle it
                # holds, so the header is left exactly as approved.
                meta_id = existing["meta_template_id"]
                status = await meta.edit_template(
                    template_id=meta_id, token=row["access_token"],
                    body_text=doc.body, sample_variables={},
                )
                tpl_category = doc.category
            else:
                meta_id, status = await meta.create_document_template(
                    waba_id=row["waba_id"], token=row["access_token"],
                    name=doc.name, language=doc.language,
                    category=doc.category, body_text=doc.body,
                    example_url=settings.meta_receipt_sample_url,
                )
                tpl_category = doc.category
        except meta.MetaError as exc:
            # Same adopt rule as the catalogue: the name may already exist on
            # that WABA (Meta re-categorised it on review, or the create's row
            # was lost), and refusing would mean the sweep retried the
            # identical create hourly forever.
            adopted = None
            if not existing:
                adopted = await meta.fetch_template(
                    waba_id=row["waba_id"], name=doc.name,
                    token=row["access_token"],
                )
            if not adopted or not adopted.id:
                logger.warning("whatsapp.template_push_failed shop=%s key=%s err=%s",
                               shop_id, key, exc)
                failed.append(key)
                continue
            logger.warning("whatsapp.template_adopted shop=%s key=%s status=%s "
                           "category=%s (create refused: %s)",
                           shop_id, key, adopted.status, adopted.category, exc)
            meta_id, status = adopted.id, adopted.status
            wanted = body_hash(adopted.body)
            tpl_category = adopted.category or doc.category
        await wq.upsert_template(
            shop_id=shop_id, template_key=key, name=doc.name,
            meta_template_id=meta_id, language=doc.language,
            category=tpl_category, status=TEMPLATE_STATUS.get(status, "pending"),
            variable_count=0, body_hash=wanted,
        )
        if existing:
            edited += 1
        else:
            created += 1

    return {"ok": True, "created": created, "edited": edited,
            "failed": failed, "not_ready": not_ready}


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
              "propagated": 0, "edited": 0, "errors": 0}

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

    # Live senders missing part of the catalogue — or the document template —
    # or holding an outdated body. The fingerprints include the receipt since
    # 2026-09-23, so a shop without it is on the worklist like any other gap.
    # Skipped entirely when the gate is empty — there is nothing to give them,
    # and asking would be one pointless query per shop per hour.
    if approved:
        for row in await wq.list_senders_needing_templates(propagation_fingerprints()):
            try:
                result = await ensure_templates(
                    shop_id=row["shop_id"], settings=settings, approved=approved
                )
                pushed = result.get("created", 0) + result.get("edited", 0)
                if pushed:
                    counts["propagated"] += pushed
                    counts["edited"] += result.get("edited", 0)
                    logger.info(
                        "whatsapp.templates_propagated_late shop=%s created=%s edited=%s",
                        row["shop_id"], result["created"], result["edited"],
                    )
            except Exception:  # noqa: BLE001 — see above
                logger.exception("whatsapp.late_propagation_failed shop=%s",
                                 row["shop_id"])
                counts["errors"] += 1

    for row in await wq.list_senders_needing_sync():
        try:
            if not await sync_coexistence(row):
                counts["errors"] += 1
        except Exception:  # noqa: BLE001 — see above
            logger.exception("whatsapp.coexistence_sync_sweep_failed shop=%s", row["shop_id"])
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

    # The renewal nudge, by email. Meta renews nothing, so a salon that does
    # not reconnect simply stops sending on day 61 — and the banner that used
    # to be the only warning is pull-only: the owner has to open the app inside
    # the window, and never sees it while their session is in employee view.
    # The attempt is recorded whether or not the mail went, so a salon with no
    # owner mailbox is not retried every hour against a fact about the shop.
    for row in await wq.list_senders_needing_token_reminder(
        window_days=RENEW_WINDOW_DAYS, cooldown_hours=REMINDER_COOLDOWN_HOURS,
    ):
        try:
            await webapp_notify.whatsapp_token_expiring(
                shop_id=row["shop_id"], days_left=row["days_left"],
                phone_number=row.get("phone_number"), settings=settings,
            )
            await wq.mark_token_reminder_sent(row["shop_id"])
            counts["reminded"] = counts.get("reminded", 0) + 1
        except Exception:  # noqa: BLE001 — see above
            logger.exception("whatsapp.token_reminder_failed shop=%s", row["shop_id"])
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
