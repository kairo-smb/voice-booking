"""WhatsApp onboarding, campaigns, and Twilio webhooks.

Management endpoints are control-plane authenticated (the webapp is the only
caller, same as /sms/send). The three webhooks are authenticated by Twilio
signature instead — and by the *subaccount's* token, since that's the account
that owns the traffic.
"""
from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from booking_engine.api.deps import require_control_plane_token, _get_settings
from booking_engine.config import Settings
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import whatsapp_onboarding as onboarding
from booking_engine.services.messaging.whatsapp_send import enqueue_campaign
from booking_engine.services.messaging.whatsapp_templates import CATALOGUE
from booking_engine.services.twilio_signature import twilio_signature_valid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

# Twilio message status -> ours. 'read' is WhatsApp-only and worth keeping:
# it's the one delivery signal SMS never had.
_STATUS_MAP = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "failed",
}

# Meta refused because the recipient opted out of marketing. The only two
# error codes that mean "stop asking", as opposed to "try again later".
_OPT_OUT_CODES = {"63033", "63050"}


class StartRequest(BaseModel):
    shop_id: UUID
    display_name: str = Field(min_length=1, max_length=120)
    source: str = "kairo"
    phone_number: str | None = None


class WabaRequest(BaseModel):
    shop_id: UUID
    waba_id: str = Field(min_length=1, max_length=64)


class VerifyRequest(BaseModel):
    shop_id: UUID
    code: str = Field(min_length=4, max_length=10)


class Recipient(BaseModel):
    customer_id: UUID
    variables: dict[str, str] = Field(default_factory=dict)


class CampaignRequest(BaseModel):
    shop_id: UUID
    campaign_key: str = Field(min_length=1, max_length=80)
    template_key: str = "promo_v1"
    recipients: list[Recipient] = Field(min_length=1, max_length=500)


# ------------------------------------------------------------------ onboarding

@router.post("/onboarding/start")
async def start(
    payload: StartRequest,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Step 1: create the salon's subaccount, return its Embedded Signup config."""
    result = await onboarding.start(
        shop_id=payload.shop_id, display_name=payload.display_name,
        source=payload.source, phone_number=payload.phone_number,
        settings=settings,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return {"data": result}


@router.post("/onboarding/waba")
async def waba(
    payload: WabaRequest,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Step 2: the salon finished Meta's popup; register the sender."""
    result = await onboarding.attach_waba(
        shop_id=payload.shop_id, waba_id=payload.waba_id, settings=settings
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return {"data": result}


@router.post("/onboarding/verify")
async def verify(
    payload: VerifyRequest,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Step 3 (source='salon' only): the salon types Meta's OTP.

    For source='kairo' the OTP lands on a number we own and
    /whatsapp/webhook/otp submits it without anyone typing anything.
    """
    result = await onboarding.submit_code(
        shop_id=payload.shop_id, code=payload.code, settings=settings
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return {"data": result}


@router.get("/status/{shop_id}")
async def status(
    shop_id: UUID,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Everything the webapp needs to render the onboarding/waiting state."""
    sender = await wq.get_sender(shop_id)
    if not sender:
        return {"data": {"status": "not_started", "templates": []}}
    templates = [
        {"template_key": key,
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
        "daily_cap": sender["daily_cap"],
        "offline_reason": sender["offline_reason"],
        "sent_today": await wq.sent_today(shop_id),
        "templates": templates,
    }}


@router.post("/templates/ensure/{shop_id}")
async def ensure_templates(
    shop_id: UUID,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Re-run template creation — after a rejection, or a catalogue addition."""
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
    """Queue a personalised campaign, spread across the salon's opening hours.

    Returns immediately with the schedule. Nothing is sent inline: 50 sends
    would be 50 serial Twilio calls with an owner watching a spinner, and the
    whole point is that they land through the day, not at once.
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


@router.delete("/campaigns/{shop_id}/{campaign_key}")
async def cancel_campaign(
    shop_id: UUID,
    campaign_key: str,
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Cancel whatever hasn't gone out yet. Sent rows are untouched history."""
    cancelled = await wq.cancel_queued(shop_id=shop_id, campaign_key=campaign_key)
    return {"data": {"cancelled": cancelled}}


# -------------------------------------------------------------------- webhooks

async def _verified_form(request: Request, settings: Settings, path: str) -> dict | None:
    """Parse and signature-check a Twilio webhook, or None if it isn't genuine."""
    form = dict(await request.form())
    sender = await wq.get_sender_by_subaccount(form.get("AccountSid", ""))
    token = (sender or {}).get("subaccount_auth_token")
    if not twilio_signature_valid(
        request, form, settings, path=path, auth_token=token
    ):
        return None
    return form


@router.post("/webhook/status")
async def status_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(_get_settings)],
) -> Response:
    form = await _verified_form(request, settings, "/api/v1/whatsapp/webhook/status")
    if form is None:
        return Response(status_code=403)

    mapped = _STATUS_MAP.get(form.get("MessageStatus", ""))
    if mapped:
        price = form.get("Price")
        error_code = form.get("ErrorCode") or None
        row = await wq.update_status_by_sid(
            provider_sid=form.get("MessageSid", ""),
            status=mapped,
            price_usd=abs(float(price)) if price else None,
            error_code=error_code,
        )
        # An opt-out is the customer using Meta's own "Stop promotions"
        # button — the self-service opt-out the SMS channel doesn't have.
        # Recording it in business_app_core keeps the webapp's consent UI
        # honest and stops the next campaign burning a send on a certain
        # failure.
        if row and error_code in _OPT_OUT_CODES and row.get("customer_id"):
            await wq.withdraw_marketing_consent(row["customer_id"])
            logger.info(
                "whatsapp.opt_out shop=%s customer=%s code=%s",
                row["shop_id"], row["customer_id"], error_code,
            )
    return Response(content=_EMPTY_TWIML, media_type="application/xml")


@router.post("/webhook/inbound")
async def inbound_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(_get_settings)],
) -> Response:
    """A customer replied. Logged only — the reply itself opens Meta's 24h
    session window, which nothing in this repo uses yet."""
    form = await _verified_form(request, settings, "/api/v1/whatsapp/webhook/inbound")
    if form is None:
        return Response(status_code=403)
    logger.info(
        "whatsapp.inbound from=%s account=%s",
        form.get("From", ""), form.get("AccountSid", ""),
    )
    return Response(content=_EMPTY_TWIML, media_type="application/xml")


@router.post("/webhook/otp")
async def otp_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(_get_settings)],
) -> Response:
    """Meta's ownership OTP, arriving as an SMS on a Kairo-owned number.

    Signed with the *parent* token: the number lives in the parent account,
    unlike the WhatsApp traffic above. Bound only while a sender is
    verifying, and unbound again the moment it goes online.
    """
    form = dict(await request.form())
    if not twilio_signature_valid(
        request, form, settings, path="/api/v1/whatsapp/webhook/otp"
    ):
        return Response(status_code=403)
    used = await onboarding.handle_otp_sms(
        to_number=form.get("To", ""),
        body=form.get("Body", ""),
        settings=settings,
    )
    logger.info("whatsapp.otp_webhook used=%s", used)
    return Response(content=_EMPTY_TWIML, media_type="application/xml")
