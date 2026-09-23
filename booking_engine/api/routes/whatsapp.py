"""WhatsApp onboarding, campaigns, and Meta webhooks.

Management endpoints are control-plane authenticated (the webapp is the only
caller, same as /sms/send). The webhook is authenticated by Meta's
`X-Hub-Signature-256` over the raw body, with the **app secret** — one secret
for every customer's traffic, unlike Twilio's per-account signing, which is
why migration 15 dropped the per-subaccount token column.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request,
)
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from booking_engine.api.deps import require_control_plane_token, _get_settings
from booking_engine.clients import meta_whatsapp as meta
from booking_engine.config import Settings
from booking_engine.db import whatsapp_audit_queries as waq
from booking_engine.db import whatsapp_automation_queries as aq
from booking_engine.db import whatsapp_queries as wq
from booking_engine.db import wa_session_queries as wsq
from booking_engine.db import whatsapp_thread_queries as tq
from booking_engine.services.messaging import meta_limits
from booking_engine.services.messaging import wa_agent
from booking_engine.services.messaging import wa_inbound
from booking_engine.services.messaging import whatsapp_onboarding as onboarding
from booking_engine.services.messaging.whatsapp_pricing import price_list
from booking_engine.services.messaging import whatsapp_receipt
from booking_engine.services.messaging.whatsapp_send import enqueue_campaign
from booking_engine.services.messaging.whatsapp_onboarding import template_name
from booking_engine.services.messaging.whatsapp_templates import (
    CATALOGUE, DEFAULT_LANGUAGE, RECEIPT_TEMPLATE_NAME, resolve_language,
)
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


def _template_descriptor(key: str, language: str = DEFAULT_LANGUAGE) -> dict:
    """Everything a caller needs to render the picker AND build the prompt.

    One payload for both so they cannot disagree: the picker showing
    `winback_v1` while the generator writes for `promo_v1`'s frame would
    produce grammatically broken messages with nothing failing.

    `language` is the shop's own locale, already resolved to one we have copy
    for. marketing-engine writes the generated slot in whatever this says
    (`buildOfferSystem` reads it straight off the descriptor), so a wrong value
    here is a message in the wrong language, not an error anywhere.
    """
    tpl = CATALOGUE[key]
    return {
        "template_key": key,
        "name": template_name(key, language),
        "body": tpl.body,
        "category": tpl.category,
        "language": language,
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
    # Acting staff id, propagated by the webapp (its JWT sub) so the audit
    # records who did this. Sent only by the webapp; absent = system.
    requested_by: UUID | None = None
    display_name: str = Field(min_length=1, max_length=120)
    # Token refresh, not a first connection: the salon is already online and
    # is redoing the popup before its 60-day token expires.
    reconnect: bool = False


class CompleteRequest(BaseModel):
    """What Meta's Embedded Signup popup hands back to the browser.

    Only `code` is required. The ids ride on a `WA_EMBEDDED_SIGNUP`
    postMessage that Meta sends solely through its JS SDK, which this flow no
    longer uses, so in practice the browser has nothing else to send — the
    service reads both back from the exchanged token instead. Kept accepted,
    not removed: they are still correct when present, and refusing them would
    break any caller that does go through the SDK.
    """

    shop_id: UUID
    requested_by: UUID | None = None
    # Absent on the second call, which answers a `waba_ambiguous` by naming the
    # WABA. The code is single-use and already spent by then; the service
    # resumes from the token it persisted rather than asking for another popup.
    code: str | None = Field(default=None, max_length=512)
    waba_id: str | None = Field(default=None, max_length=64)
    phone_number_id: str | None = Field(default=None, max_length=64)
    # The origin the dialog was opened with. Meta binds the code to it and
    # refuses an exchange that does not repeat it verbatim, so it comes from
    # the browser that built the URL rather than from config here — the webapp
    # is served on several origins and this service knows none of them.
    redirect_uri: str | None = Field(default=None, max_length=512)
    reconnect: bool = False


class Recipient(BaseModel):
    customer_id: UUID
    variables: dict[str, str] = Field(default_factory=dict)


class AutomationRuleRequest(BaseModel):
    shop_id: UUID
    requested_by: UUID | None = None
    rule_key: str
    enabled: bool
    params: dict[str, str | int] = Field(default_factory=dict)


class CampaignRequest(BaseModel):
    shop_id: UUID
    requested_by: UUID | None = None
    # Which webapp surface enqueued this: composer (AI) | touchpoint (bulk) |
    # offer (single win-back). NULL if an operator ever calls the API directly.
    source: str | None = None
    campaign_key: str = Field(min_length=1, max_length=80)
    template_key: str = "promo_v1"
    # 2000 rather than the old 500: a bulk send to the whole consenting book is
    # the point of the Touchpoint tile, and `spread` now lays a campaign across
    # as many days as the daily cap needs. The monthly plan allowance is the
    # real ceiling and is enforced in enqueue_campaign.
    recipients: list[Recipient] = Field(min_length=1, max_length=2000)


class ReceiptRequest(BaseModel):
    """Smart Receipt send. The template is fixed server-side to Meta's
    `purchase_receipt_1`; the webapp's `template_key` field is informational and
    ignored here (Pydantic drops unknown fields)."""

    shop_id: UUID
    customer_id: UUID
    phone: str = Field(min_length=1, max_length=32)
    payment_id: str = Field(min_length=1, max_length=80)
    reference: str = ""
    filename: str = "ricevuta.pdf"
    pdf_base64: str = Field(min_length=1)
    requested_by: UUID | None = None
    source: str | None = None


class ReplyRequest(BaseModel):
    """A free-form answer typed by the owner in the Inbox.

    `phone` is whatever spelling the caller holds — with or without the leading
    '+'. Nothing here normalises it: the thread SQL matches on
    `ltrim(phone,'+')` on both sides, and a second normalisation on this side
    would be a second rule to keep in agreement with that one.
    """

    shop_id: UUID
    phone: str = Field(min_length=1, max_length=32)
    # Meta's own text ceiling is 4096 characters; a longer body is rejected by
    # Graph, so it is refused here where the owner can see why.
    body: str = Field(max_length=4096)


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
        settings=settings, reconnect=payload.reconnect,
    )
    await waq.record_audit_event(
        shop_id=payload.shop_id, event="onboarding.start",
        actor_id=payload.requested_by,
        response=result if result.get("ok") else None,
        status="success" if result.get("ok") else "error",
        http_status=None if result.get("ok") else 409,
        error_message=result.get("error") if not result.get("ok") else None,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return {"data": result}


@router.post("/onboarding/complete")
async def complete(
    payload: CompleteRequest,
    background: BackgroundTasks,
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
        redirect_uri=payload.redirect_uri, reconnect=payload.reconnect,
    )
    # The single-use `code` never reaches the audit — only the identity-bearing
    # ids that say which WABA the salon connected.
    await waq.record_audit_event(
        shop_id=payload.shop_id, event="onboarding.complete",
        actor_id=payload.requested_by,
        request={"waba_id": payload.waba_id, "phone_number_id": payload.phone_number_id},
        response=result if result.get("ok") else None,
        status="success" if result.get("ok") else "error",
        http_status=None if result.get("ok") else 409,
        error_message=result.get("error") if not result.get("ok") else None,
    )
    if not result.get("ok"):
        # An ambiguity is a question, not just a refusal: the caller needs the
        # candidates in order to ask the owner, so this one carries the whole
        # result instead of the bare slug every other refusal flattens to.
        if result.get("wabas"):
            raise HTTPException(status_code=409, detail=result)
        raise HTTPException(status_code=409, detail=result.get("error"))
    # After the response, not inside it: one Graph round trip per catalogue
    # entry is enough to blow the gateway timeout in front of the webapp, and
    # the owner would see a 504 for a sender that is already online. A failure
    # here is picked up by the hourly sweep, which revisits any sender missing
    # templates — so this is a head start, not the only path.
    background.add_task(onboarding.ensure_templates,
                        shop_id=payload.shop_id, settings=settings)
    return {"data": result}


@router.delete("/onboarding/{shop_id}")
async def abort_onboarding(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
    requested_by: UUID | None = Query(default=None),
) -> dict:
    """Owner closed Meta's popup without finishing — reset so they can retry."""
    result = await onboarding.abort(shop_id=shop_id)
    await waq.record_audit_event(
        shop_id=shop_id, event="onboarding.abort", actor_id=requested_by,
        status="success",
    )
    return {"data": result}


@router.get("/status/{shop_id}")
async def status(
    shop_id: UUID,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Everything the webapp needs to render the onboarding/waiting state."""
    sender = await wq.get_sender(shop_id)
    # A pending row nobody touched for a while is an abandoned popup, not a
    # verification in flight — report it as never started so the panel offers
    # the connect button instead of a stuck "Meta is verifying" box.
    if sender and onboarding.is_abandoned(sender):
        sender["status"] = "not_started"
    language = resolve_language(await wq.get_shop_language(shop_id))
    if not sender:
        # The price list is not a sender fact: the webapp shows "what this
        # would cost you" before onboarding starts.
        return {"data": {
            "status": "not_started",
            "templates": [
                {**_template_descriptor(key, language), "status": "missing"}
                for key in CATALOGUE
            ],
            "sent_this_month": 0,
            "pricing": price_list(),
            "signup": onboarding.signup_config(settings),
        }}
    templates = [
        {**_template_descriptor(key, language),
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
        # NULL unless Meta reported an expiry on the code exchange. Nothing
        # renews it — when it passes, this sender is dead until the salon
        # redoes Embedded Signup, so the date has to be reachable.
        "token_expires_at": (
            sender["token_expires_at"].isoformat()
            if sender["token_expires_at"] else None
        ),
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
    requested_by: UUID | None = Query(default=None),
) -> dict:
    """Re-run template injection — after a rejection, or a catalogue addition."""
    result = await onboarding.ensure_templates(shop_id=shop_id, settings=settings)
    await waq.record_audit_event(
        shop_id=shop_id, event="templates.ensure", actor_id=requested_by,
        status="success" if result.get("ok") else "error",
        http_status=None if result.get("ok") else 409,
        error_message=result.get("error") if not result.get("ok") else None,
    )
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
        initiated_by=payload.requested_by,
    )
    # Recipients never go into the audit — count only; the rows belong to
    # outbound_messages.
    await waq.record_audit_event(
        shop_id=payload.shop_id, event="campaign.enqueue",
        actor_id=payload.requested_by, source=payload.source,
        campaign_key=payload.campaign_key, template_name=payload.template_key,
        is_template=True, recipient_count=len(payload.recipients),
        request={"campaign_key": payload.campaign_key, "template_key": payload.template_key},
        response=result if result.get("ok") else None,
        status="success" if result.get("ok") else "error",
        http_status=None if result.get("ok") else 409,
        error_message=result.get("error") if not result.get("ok") else None,
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


@router.post("/receipts")
async def receipt(
    payload: ReceiptRequest,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Send one receipt PDF as a WhatsApp document (Smart Receipt).

    Synchronous and immediate, unlike `campaigns`: the webapp calls this right
    after a paid ticket closes and expects the PDF uploaded and delivered now.
    The template is fixed server-side to Meta's `purchase_receipt_1`.
    """
    result = await whatsapp_receipt.send_receipt(
        shop_id=payload.shop_id, customer_id=payload.customer_id,
        phone=payload.phone, reference=payload.reference,
        filename=payload.filename, pdf_base64=payload.pdf_base64,
        initiated_by=payload.requested_by, settings=settings,
    )
    await waq.record_audit_event(
        shop_id=payload.shop_id, event="receipt.send",
        actor_id=payload.requested_by, source=payload.source,
        template_name=RECEIPT_TEMPLATE_NAME,
        request={"payment_id": payload.payment_id, "reference": payload.reference},
        response=result if result.get("ok") else None,
        status="success" if result.get("ok") else "error",
        http_status=None if result.get("ok") else 409,
        error_message=result.get("error") if not result.get("ok") else None,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return {"data": result}


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
    await waq.record_audit_event(
        shop_id=shop_id, event="automation.config", actor_id=payload.requested_by,
        request={"rule_key": payload.rule_key, "enabled": payload.enabled,
                 "params": payload.params},
        status="success",
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
    requested_by: UUID | None = Query(default=None),
    source: str | None = Query(default=None),
) -> dict:
    """Cancel whatever hasn't gone out yet. Sent rows are untouched history."""
    cancelled = await wq.cancel_queued(shop_id=shop_id, campaign_key=campaign_key)
    await waq.record_audit_event(
        shop_id=shop_id, event="campaign.cancel", actor_id=requested_by,
        source=source, campaign_key=campaign_key,
        response={"cancelled": cancelled}, status="success",
    )
    return {"data": {"cancelled": cancelled}}


# --------------------------------------------------------------------- threads

@router.get("/threads/{shop_id}")
async def threads(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """The Inbox's first screen: one row per phone, newest first.

    One query, not one per thread — the session's routed intent is derived in
    the list SQL precisely so this endpoint stays O(1) round trips. The two
    derived fields are computed here rather than in SQL because they are pure
    rules (`window_open`, `needs_attention`) with their own unit tests, and a
    second copy of either in a query is a second place to get the 24h boundary
    wrong.
    """
    rows = await tq.thread_list(shop_id)
    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        # Who is holding this thread, in the agent's own words. An owner who
        # cannot tell why the agent is quiet assumes it is broken and turns it
        # off, so every silence comes back named rather than as a bare false.
        active, reason = wa_agent.agent_status(row)
        out.append({
            **row,
            "window_open": tq.window_open(last_inbound=row.get("last_inbound"), now=now),
            "needs_attention": tq.needs_attention(row),
            "agent_active": active,
            "agent_reason": reason,
        })
    return {"data": out}


@router.post("/threads/{shop_id}/{phone}/takeover")
async def takeover(
    shop_id: UUID,
    phone: str,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """"Rispondo io": the owner takes this conversation off the agent.

    Written on the session row, because that is where the handover rules
    already read from — there is no per-thread state table and this is not the
    place to invent one. `open_session` first, since the owner may take over a
    thread the agent has not spoken on yet (a message that just arrived, or one
    it is still debouncing); with no row there would be nothing to mark, and
    the next inbound message would find a clean slate and answer anyway.

    `customer_id` is not passed: it only matters when this call *creates* the
    session, which is the case where no turn will ever run on it. Looking it up
    to write `customer_match = 'existing'` on a row that exists solely to say
    "a person has this" would be a query for a field nothing reads.

    **One direction only — there is no endpoint to hand it back.** See the
    module note on `wa_agent.TAKEOVER_REASON`: the agent resumes by itself on
    the customer's next conversation, and un-escalating this one would put it
    back into a thread a person is in the middle of.
    """
    call_id = await wsq.open_session(shop_id=shop_id, phone=phone, customer_id=None)
    await wsq.mark_escalated(call_id=call_id, reason=wa_agent.TAKEOVER_REASON)
    return {"data": {"taken_over": True}}


@router.get("/threads/{shop_id}/{phone}")
async def thread(
    shop_id: UUID,
    phone: str,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """One conversation, both directions, oldest first — and it marks it read.

    Reading the thread *is* what marks it read: the two are the same act, and a
    separate endpoint would be one more call the webapp can forget to make,
    after which the unread badge lies forever. `mark_read` is idempotent
    (`read_at IS NULL`), so there is nothing to branch on for an empty thread —
    an unknown phone returns an empty timeline rather than a 404, because "this
    customer has never written" is an answer, not an error.
    """
    messages = await tq.thread_timeline(shop_id, phone)
    await tq.mark_read(shop_id, phone)
    return {"data": {"messages": messages}}


@router.post("/reply")
async def reply(
    payload: ReplyRequest,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Free-form reply from the owner, inside Meta's 24h service window.

    **No credit debit, deliberately.** Meta does not charge for a service
    conversation (one the customer opened) and, as a Tech Provider, Kairo has
    no credit line to share — the salon's own card is on the salon's own WABA.
    A debit here would bill the salon for something nobody charges us for, so
    this path matches every other WhatsApp send in this repo and takes none.
    """
    if not payload.body.strip():
        # Graph rejects an empty text body. Refusing locally names the problem;
        # relaying Meta's error would not.
        return {"ok": False, "error": "empty_body"}

    sender = await wq.get_sender(payload.shop_id)
    if not sender or sender["status"] != "online":
        return {"ok": False, "error": "sender_offline"}

    # Before Graph, never after. Outside the window Meta answers 131047, which
    # reaches the owner as an opaque provider error they cannot act on — and
    # costs a Graph round trip to learn something we already knew.
    last_inbound = await tq.last_inbound_at(payload.shop_id, payload.phone)
    if not tq.window_open(last_inbound=last_inbound,
                          now=datetime.now(timezone.utc)):
        return {"ok": False, "error": "session_window_closed"}

    sid = await meta.send_text(
        phone_number_id=sender["phone_number_id"], to=payload.phone,
        body=payload.body, token=sender["access_token"],
    )
    try:
        await tq.record_reply(shop_id=payload.shop_id, to_phone=payload.phone,
                              body=payload.body, provider_sid=sid)
    except Exception:  # noqa: BLE001
        # Meta has already delivered it; the customer's phone has the message.
        # Reporting failure would have the owner send it a second time, which
        # is the worse of the two wrongs, so the send is reported as what it
        # is — sent, and missing from the thread. `recorded: False` is the
        # webapp's cue to say so, and the log is how it gets repaired.
        logger.exception(
            "whatsapp.reply_not_recorded shop=%s to=%s sid=%s",
            payload.shop_id, payload.phone, sid,
        )
        return {"data": {"sent": True, "provider_sid": sid, "recorded": False}}
    return {"data": {"sent": True, "provider_sid": sid}}


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


def _interactive_or_text(message: dict) -> tuple[str | None, str]:
    """(button_id, display_text). `button_id` is None for anything typed.

    A tap on a menu we sent comes back as `type: "interactive"` carrying the id
    we defined. Storing the *title* as the body is what makes the thread view
    show the customer what they saw themselves tap, rather than a slug.

    Every read is a `.get()` chain that degrades to empty: a shape Meta changes
    or a type we have never seen must record a blank message, not raise inside
    the webhook. Same reason the text branch tolerates a bare string.
    """
    if message.get("type") == "interactive":
        inter = message.get("interactive")
        if not isinstance(inter, dict):
            return None, ""
        reply = inter.get("button_reply") or inter.get("list_reply")
        if not isinstance(reply, dict):
            return None, ""
        return (str(reply.get("id") or "") or None, str(reply.get("title") or ""))
    text = message.get("text")
    if isinstance(text, dict):
        return None, str(text.get("body") or "")
    return None, str(text or "")


def _media_id(message: dict) -> str | None:
    """The attachment id of a voice note, which exists only on this payload.

    Meta nests it under a key named after the type (`audio.id`) and it is not
    a column on `inbound_messages` — the download URL it resolves to expires
    within minutes, so there would be nothing durable to store. It rides on
    the dict handed to the worker instead.

    Audio only: nothing else is transcribed, and fetching an image we cannot
    read would spend the salon's token on bytes with nowhere to go.
    """
    if message.get("type") != "audio":
        return None
    audio = message.get("audio")
    if not isinstance(audio, dict):
        return None
    return str(audio.get("id") or "") or None


async def _handle_change(sender: dict, change: dict) -> None:
    field = change.get("field")
    value = change.get("value") or {}

    if field == "message_template_status_update":
        # Minutes instead of the next hourly tick. The tick's poll survives as
        # a reconciler for the webhook Meta doesn't deliver.
        name = value.get("message_template_name", "")
        matched = await wq.set_template_status(
            shop_id=sender["shop_id"],
            name=name,
            status=onboarding.TEMPLATE_STATUS.get(
                (value.get("event") or "").lower(), "pending"
            ),
            rejection_reason=value.get("reason") or None,
        )
        # Meta ruling on a template we hold no row for: it exists on that WABA
        # and we don't know it. Silent until 2026-09-20, when six of them did
        # exactly this. The sweep's adoption path is what repairs it — this
        # only makes the gap audible in the meantime.
        if not matched:
            logger.warning(
                "whatsapp.template_verdict_unmatched shop=%s name=%s event=%s",
                sender["shop_id"], name, value.get("event"),
            )
        return

    # Meta named the coexistence field `smb_message_echoes`; the array inside
    # it is `message_echoes`. Both names are accepted because the field name is
    # the one thing here confirmed only from a BSP's mirror of Meta's docs, and
    # accepting a name we never receive costs nothing.
    if field in ("smb_message_echoes", "message_echoes"):
        # The owner answers from the WhatsApp Business App and Meta reports it
        # here. Recorded so the thread is not a half-conversation and the owner
        # is not asked to answer something they already answered.
        #
        # Deliberately does NOT touch the 24h window: that is driven by
        # customer inbound alone, and an echo that extended it would let us
        # send into a conversation Meta considers closed.
        for echo in value.get("message_echoes") or []:
            await wq.record_echo(
                shop_id=sender["shop_id"],
                to_phone=str(echo.get("to") or ""),
                # Meta reports the business number as the echo's sender; the
                # row on file is the fallback for a payload that omits it.
                from_number=str(echo.get("from") or sender.get("phone_number") or ""),
                body=_interactive_or_text(echo)[1],
                wa_message_id=str(echo.get("id") or "") or None,
            )
            logger.info(
                "whatsapp.echo shop=%s to=%s type=%s",
                sender["shop_id"], echo.get("to"), echo.get("type"),
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
        button_id, text = _interactive_or_text(message)
        row = await wq.record_inbound(
            shop_id=sender["shop_id"],
            from_phone=str(message.get("from") or ""),
            body=text,
            message_type=str(message.get("type") or "text"),
            wa_message_id=str(message.get("id") or "") or None,
            # A tap is already named: the id is one we defined, so it *is* the
            # intent and there is nothing for a model to rule on.
            intent=button_id,
            confidence=1.0 if button_id else None,
        )
        if row is None:
            # Meta replayed a webhook we have already recorded. Skipping here
            # is what keeps a retry from costing a second AI classification.
            logger.info(
                "whatsapp.inbound_replay shop=%s wamid=%s",
                sender["shop_id"], message.get("id"),
            )
            continue
        logger.info(
            "whatsapp.inbound shop=%s from=%s type=%s intent=%s",
            sender["shop_id"], message.get("from"), message.get("type"), button_id,
        )
        # Everything slow — media, transcription, the classifier — happens
        # after this handler has returned and Meta has its 200. Only a fresh
        # row gets one: scheduling on a replay would hand back the exact cost
        # the dedup above exists to avoid.
        wa_inbound.schedule(sender, {**row, "media_id": _media_id(message)})
