"""WhatsApp onboarding, campaigns, and Meta webhooks.

Management endpoints are control-plane authenticated (the webapp is the only
caller, same as /sms/send). The webhook is authenticated by Meta's
`X-Hub-Signature-256` over the raw body, with the **app secret** — one secret
for every customer's traffic, unlike Twilio's per-account signing, which is
why migration 15 dropped the per-subaccount token column.
"""
from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from booking_engine.api.deps import require_control_plane_token, _get_settings
from booking_engine.config import Settings
from booking_engine.db import whatsapp_automation_queries as aq
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import meta_limits
from booking_engine.services.messaging import whatsapp_onboarding as onboarding
from booking_engine.services.messaging.whatsapp_pricing import price_list
from booking_engine.services.messaging.whatsapp_send import enqueue_campaign
from booking_engine.services.messaging.whatsapp_templates import CATALOGUE
from booking_engine.services.meta_signature import meta_signature_valid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

# Meta message status -> ours. 'read' is WhatsApp-only and worth keeping: it's
# the one delivery signal SMS never had.
_STATUS_MAP = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
}

# The recipient used Meta's native "Stop promotions" button — the self-service
# opt-out this channel has and SMS doesn't. Distinct from 131049, the
# cross-brand frequency cap, which is *not* an opt-out and is retried by
# whatsapp_send rather than recorded here.
_OPT_OUT_CODES = {131050}


def _template_descriptor(key: str) -> dict:
    """Everything a caller needs to render the picker AND build the prompt.

    One payload for both so they cannot disagree: the picker showing
    `winback_v1` while the generator writes for `promo_v1`'s frame would
    produce grammatically broken messages with nothing failing.
    """
    tpl = CATALOGUE[key]
    return {
        "template_key": key,
        "body": tpl.body,
        "category": tpl.category,
        "language": tpl.language,
        "variables": tpl.variables,
        "generated_slot": tpl.generated_slot,
        "filled_by": tpl.filled_by,
        "max_chars": tpl.max_chars,
        "intent": tpl.intent,
        "guidance": tpl.guidance,
        # The approved sample per variable, so the webapp can render the body
        # with realistic values (the automations tile shows the owner exactly
        # what the customer will read) without duplicating the catalogue.
        "sample": tpl.sample,
    }


class StartRequest(BaseModel):
    shop_id: UUID
    display_name: str = Field(min_length=1, max_length=120)


class CompleteRequest(BaseModel):
    """Everything Meta's Embedded Signup popup hands back to the browser."""

    shop_id: UUID
    code: str = Field(min_length=1, max_length=512)
    waba_id: str = Field(min_length=1, max_length=64)
    phone_number_id: str = Field(min_length=1, max_length=64)


class Recipient(BaseModel):
    customer_id: UUID
    variables: dict[str, str] = Field(default_factory=dict)


class AutomationRuleRequest(BaseModel):
    shop_id: UUID
    rule_key: str
    enabled: bool
    params: dict[str, str | int] = Field(default_factory=dict)


class CampaignRequest(BaseModel):
    shop_id: UUID
    campaign_key: str = Field(min_length=1, max_length=80)
    template_key: str = "promo_v1"
    # 2000 rather than the old 500: a bulk send to the whole consenting book is
    # the point of the Touchpoint tile, and `spread` now lays a campaign across
    # as many days as the daily cap needs. The monthly plan allowance is the
    # real ceiling and is enforced in enqueue_campaign.
    recipients: list[Recipient] = Field(min_length=1, max_length=2000)


# ------------------------------------------------------------------ onboarding

@router.post("/onboarding/start")
async def start(
    payload: StartRequest,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Step 1: record intent, hand back the Embedded Signup config.

    Creates nothing provider-side — the Twilio version had to create a
    subaccount here and leaked one on every abandoned onboarding.
    """
    result = await onboarding.start(
        shop_id=payload.shop_id, display_name=payload.display_name,
        settings=settings,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return {"data": result}


@router.post("/onboarding/complete")
async def complete(
    payload: CompleteRequest,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Step 2 and last: the salon finished Meta's popup.

    Exchanges the code, subscribes to the WABA's webhooks, verifies the number
    with Meta rather than trusting the popup, and injects the templates.
    """
    result = await onboarding.complete(
        shop_id=payload.shop_id, code=payload.code, waba_id=payload.waba_id,
        phone_number_id=payload.phone_number_id, settings=settings,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return {"data": result}


@router.get("/status/{shop_id}")
async def status(
    shop_id: UUID,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Everything the webapp needs to render the onboarding/waiting state."""
    sender = await wq.get_sender(shop_id)
    if not sender:
        # The price list is not a sender fact: the webapp shows "what this
        # would cost you" before onboarding starts.
        return {"data": {
            "status": "not_started",
            "templates": [
                {**_template_descriptor(key), "status": "missing"}
                for key in CATALOGUE
            ],
            "sent_this_month": 0,
            "pricing": price_list(),
            "signup": onboarding.signup_config(settings),
        }}
    templates = [
        {**_template_descriptor(key),
         "status": (await wq.get_template(shop_id, key) or {}).get("status", "missing")}
        for key in CATALOGUE
    ]
    return {"data": {
        "status": sender["status"],
        "source": sender["source"],
        "phone_number": sender["phone_number"],
        "display_name": sender["display_name"],
        "quality_rating": sender["quality_rating"],
        "messaging_limit": sender["messaging_limit"],
        # Meta's own answer to "did the salon keep their WhatsApp Business
        # App?" — the promise this whole migration is selling.
        "coexistence": sender["platform_type"] == "COEXISTENCE",
        # The binding ceiling — min(Meta's tier, our drip rate) — not the raw
        # `daily_cap` column. Showing our number when Meta's is lower would
        # promise the owner throughput we will refuse to deliver.
        "daily_cap": meta_limits.effective_daily_cap(sender),
        "configured_daily_cap": sender["daily_cap"],
        "meta_tier": sender["messaging_limit"],
        "meta_tier_daily": meta_limits.tier_daily_conversations(
            sender["messaging_limit"]
        ),
        "recipient_cooldown_hours": settings.whatsapp_recipient_cooldown_hours,
        "offline_reason": sender["offline_reason"],
        "sent_today": await wq.sent_today(shop_id),
        "sent_last_24h": await wq.sent_last_24h(shop_id),
        # Marketing only, both of them: an appointment reminder is not a
        # promotion and must not show up in the owner's campaign counter.
        "sent_this_month": await wq.sent_this_month(shop_id),
        "pricing": price_list(),
        "templates": templates,
        "signup": onboarding.signup_config(settings),
    }}


@router.post("/templates/ensure/{shop_id}")
async def ensure_templates(
    shop_id: UUID,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Re-run template injection — after a rejection, or a catalogue addition."""
    result = await onboarding.ensure_templates(shop_id=shop_id, settings=settings)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return {"data": result}


# ------------------------------------------------------------------- campaigns

@router.post("/campaigns")
async def campaign(
    payload: CampaignRequest,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Queue a personalised campaign, dripped across the salon's opening hours.

    Returns immediately with the schedule. Nothing is sent inline: a bulk send
    is hundreds of serial Graph calls with an owner watching a spinner, and the
    whole point is that they land through the day (or the week), not at once.
    """
    result = await enqueue_campaign(
        shop_id=payload.shop_id,
        campaign_key=payload.campaign_key,
        template_key=payload.template_key,
        recipients=[r.model_dump() for r in payload.recipients],
        settings=settings,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return {"data": result}


@router.get("/campaigns/{shop_id}/{campaign_key}")
async def campaign_status(
    shop_id: UUID,
    campaign_key: str,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Progress of a drip that may run for days. Polled by the bulk tile."""
    return {"data": await wq.campaign_progress(
        shop_id=shop_id, campaign_key=campaign_key
    )}


# ----------------------------------------------------------------- automations

# The two rules the automations tile offers. Absent rows mean "off" — a shop
# that has never opened the screen sends nothing — so GET always returns both,
# with defaults, rather than only the rows that happen to exist.
_RULE_DEFAULTS = {
    "feedback": {"params": {"hours_after": 24, "platform": "general", "link": ""}},
    "reminder": {"params": {"min_no_shows": 0}},
}


@router.get("/automations/{shop_id}")
async def automations(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Both rules with their params."""
    rules = {r["rule_key"]: r for r in await aq.get_rules(shop_id)}
    return {"data": {
        key: {
            "rule_key": key,
            "enabled": (rules.get(key) or _RULE_DEFAULTS[key]).get("enabled", False),
            "params": (rules.get(key) or _RULE_DEFAULTS[key])["params"],
        }
        for key in _RULE_DEFAULTS
    }}


@router.put("/automations/{shop_id}")
async def put_automation(
    shop_id: UUID,
    payload: AutomationRuleRequest,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Upsert one rule. Owner-configured, so the toggle and the rails land here."""
    if payload.shop_id != shop_id:
        raise HTTPException(status_code=422, detail="shop_id mismatch")
    if payload.rule_key not in _RULE_DEFAULTS:
        raise HTTPException(status_code=422, detail="unknown rule_key")
    row = await aq.upsert_rule(
        shop_id=shop_id, rule_key=payload.rule_key, enabled=payload.enabled,
        params=payload.params,
    )
    return {"data": {
        "rule_key": row["rule_key"],
        "enabled": row["enabled"],
        "params": row["params"],
    }}


@router.delete("/campaigns/{shop_id}/{campaign_key}")
async def cancel_campaign(
    shop_id: UUID,
    campaign_key: str,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Cancel whatever hasn't gone out yet. Sent rows are untouched history."""
    cancelled = await wq.cancel_queued(shop_id=shop_id, campaign_key=campaign_key)
    return {"data": {"cancelled": cancelled}}


# --------------------------------------------------------------------- webhook

@router.get("/messages/{shop_id}")
async def messages(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
    customer_id: UUID = Query(...),
) -> dict:
    """Per-customer history: every message sent to this person, plus the
    campaigns they were holdout of. Feeds the webapp's Anagrafiche "Campagne"
    tab, which doubles as the GDPR subject-access artifact. Owner-only in the
    webapp (the /whatsapp prefix is in OWNER_ONLY_PREFIXES)."""
    rows = await wq.customer_campaign_messages(shop_id=shop_id, customer_id=customer_id)
    return {"data": rows}


@router.get("/webhook")
async def verify_webhook(
    settings: Annotated[Settings, Depends(_get_settings)],
    mode: Annotated[str, Query(alias="hub.mode")] = "",
    token: Annotated[str, Query(alias="hub.verify_token")] = "",
    challenge: Annotated[str, Query(alias="hub.challenge")] = "",
) -> Response:
    """Meta's one-time webhook handshake: echo the challenge, or refuse."""
    if mode == "subscribe" and token and token == settings.meta_verify_token:
        return PlainTextResponse(challenge)
    return Response(status_code=403)


@router.post("/webhook")
async def webhook(
    request: Request,
    settings: Annotated[Settings, Depends(_get_settings)],
) -> Response:
    """Every customer's WhatsApp traffic arrives here, on one app-level URL.

    Meta identifies the tenant only by `entry[].id` — the WABA id — so that is
    the sole route from a payload to a shop. Always answers 200 on a genuine
    request: Meta retries on anything else and will disable a webhook that
    keeps failing, which would silently cost us every delivery status and
    every opt-out.
    """
    body = await request.body()
    if not meta_signature_valid(
        body, request.headers.get("X-Hub-Signature-256"), settings.meta_app_secret
    ):
        return Response(status_code=403)

    try:
        payload = await request.json()
    except ValueError:
        return Response(status_code=200)

    for entry in payload.get("entry") or []:
        sender = await wq.get_sender_by_waba(entry.get("id", ""))
        if not sender:
            logger.info("whatsapp.webhook_unknown_waba waba=%s", entry.get("id"))
            continue
        for change in entry.get("changes") or []:
            try:
                await _handle_change(sender, change)
            except Exception:  # noqa: BLE001 — one bad event must not 500 the batch
                logger.exception(
                    "whatsapp.webhook_change_failed shop=%s field=%s",
                    sender["shop_id"], change.get("field"),
                )
    return Response(status_code=200)


async def _handle_change(sender: dict, change: dict) -> None:
    field = change.get("field")
    value = change.get("value") or {}

    if field == "message_template_status_update":
        # Minutes instead of the next hourly tick. The tick's poll survives as
        # a reconciler for the webhook Meta doesn't deliver.
        await wq.set_template_status(
            shop_id=sender["shop_id"],
            name=value.get("message_template_name", ""),
            status=onboarding.TEMPLATE_STATUS.get(
                (value.get("event") or "").lower(), "pending"
            ),
            rejection_reason=value.get("reason") or None,
        )
        return

    if field != "messages":
        return

    for status in value.get("statuses") or []:
        mapped = _STATUS_MAP.get(status.get("status", ""))
        if not mapped:
            continue
        errors = status.get("errors") or []
        code = errors[0].get("code") if errors else None
        row = await wq.update_status_by_sid(
            provider_sid=status.get("id", ""),
            status=mapped,
            error_code=str(code) if code else None,
        )
        if row and code in _OPT_OUT_CODES and row.get("customer_id"):
            # Recording it in business_app_core keeps the webapp's consent UI
            # honest and stops the next campaign burning a send on a certain
            # failure.
            await wq.withdraw_marketing_consent(row["customer_id"])
            logger.info(
                "whatsapp.opt_out shop=%s customer=%s code=%s",
                row["shop_id"], row["customer_id"], code,
            )

    for message in value.get("messages") or []:
        # A reply opens Meta's 24h session window. Persisted (not just logged)
        # because campaign measurement needs "did this recipient reply within
        # 72h" as a queryable signal; a reply is matched back to the message it
        # answers by phone number.
        await wq.record_inbound(
            shop_id=sender["shop_id"],
            from_phone=str(message.get("from") or ""),
            body=str(message.get("text", {}).get("body") if isinstance(message.get("text"), dict) else (message.get("text") or "")),
            message_type=str(message.get("type") or "text"),
        )
        logger.info(
            "whatsapp.inbound shop=%s from=%s type=%s",
            sender["shop_id"], message.get("from"), message.get("type"),
        )
