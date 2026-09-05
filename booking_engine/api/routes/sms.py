"""SMS send + Twilio webhooks. See docs/messaging-design.md §6.1."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from booking_engine.api.deps import require_control_plane_token, _get_settings
from booking_engine.config import Settings
from booking_engine.db import sms_queries
from booking_engine.services.messaging.sms_send import send_marketing_sms
from booking_engine.services.twilio_signature import twilio_signature_valid

router = APIRouter(prefix="/sms", tags=["sms"])

# Twilio expects TwiML or an empty 200; anything else shows up as an error in
# the console and triggers retries.
_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


class SendRequest(BaseModel):
    shop_id: UUID
    customer_id: UUID
    body: str = Field(min_length=1, max_length=1600)

    @field_validator("body")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be blank")
        return v


@router.post("/send")
async def send(
    payload: SendRequest,
    settings: Annotated[Settings, Depends(_get_settings)],
    _auth: Annotated[bool, Depends(require_control_plane_token)],
) -> dict:
    """Synchronous single send: the owner is watching a modal."""
    result = await send_marketing_sms(
        shop_id=payload.shop_id,
        customer_id=payload.customer_id,
        body=payload.body,
        settings=settings,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        public_base_url=settings.public_base_url,
    )
    if not result.ok:
        # 409, not 400: the request was valid, the current state refuses it.
        raise HTTPException(status_code=409, detail=result.reason)
    return {"data": {
        "message_id": str(result.message_id),
        "segments": result.segments,
        "credits": result.credits,
    }}


@router.post("/webhook/status")
async def status(
    request: Request,
    settings: Annotated[Settings, Depends(_get_settings)],
) -> Response:
    form = dict(await request.form())
    if not twilio_signature_valid(
        request, form, settings, path="/api/v1/sms/webhook/status"
    ):
        return Response(status_code=403)

    raw = form.get("MessageStatus", "")
    mapped = {
        "delivered": "delivered",
        "failed": "failed",
        "undelivered": "failed",
        "sent": "sent",
    }.get(raw)
    if mapped:
        price = form.get("Price")
        await sms_queries.update_status_by_sid(
            provider_sid=form.get("MessageSid", ""),
            status=mapped,
            price_usd=abs(float(price)) if price else None,
            error_code=form.get("ErrorCode") or None,
        )
    return Response(content=_EMPTY_TWIML, media_type="application/xml")
