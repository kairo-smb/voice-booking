"""Dynamic TwiML webhook — per-call routing decision.

Twilio calls this on every inbound call. We respond with either:
- <Dial><Sip>OpenAI SIP endpoint</Sip></Dial> when AI is attached
- <Dial>fallback_number</Dial> when AI is detached and fallback is set
- <Say>recorded message</Say> otherwise

The decision is based on shop_config.enabled and basket balance vs. min_session_reserve.
"""
from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from twilio.request_validator import RequestValidator

from booking_engine.config import Settings, get_settings
from booking_engine.db.voice_config_queries import get_config
from booking_engine.db.voice_telephony_queries import get_telephony_by_kairo_number
from booking_engine.services.phone_normalize import digits_only
from booking_engine.services.token_meter import decide_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/voice/twiml", tags=["voice-twiml"])


_SAY_UNAVAILABLE = (
    "Salve, in questo momento il salone non è raggiungibile. "
    "La preghiamo di richiamare. Grazie."
)


def _wrap(body: str) -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
        media_type="application/xml",
    )


def _say_unavailable() -> Response:
    return _wrap(f'<Say voice="alice" language="it-IT">{_SAY_UNAVAILABLE}</Say>')


def _dial_sip(shop_id: UUID, settings: Settings) -> Response:
    sip_uri = (
        f"sip:{settings.openai_sip_project_id};X-Shop-Id={shop_id}"
        f"@sip.api.openai.com"
    )
    return _wrap(f"<Dial><Sip>{sip_uri}</Sip></Dial>")


def _dial_fallback(number: str) -> Response:
    return _wrap(f'<Dial timeout="25">{number}</Dial>')


def _twilio_signature_valid(request: Request, form: dict, settings: Settings) -> bool:
    """Verify X-Twilio-Signature; no-op until TWILIO_AUTH_TOKEN is provisioned."""
    if not settings.twilio_auth_token:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{settings.public_base_url}/api/v1/voice/twiml/incoming"
    return RequestValidator(settings.twilio_auth_token).validate(url, form, signature)


@router.post("/incoming")
async def incoming(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Twilio fires this on every inbound call."""
    form = dict(await request.form())
    if not _twilio_signature_valid(request, form, settings):
        logger.warning("twiml.incoming: invalid Twilio signature")
        return Response(status_code=403)

    called = form.get("Called", "")
    call_sid = form.get("CallSid", "")

    telephony = await get_telephony_by_kairo_number(called)
    if not telephony:
        logger.warning("twiml.incoming: unknown number %s sid=%s", called, call_sid)
        return _say_unavailable()

    shop_id: UUID = telephony["shop_id"]
    config = await get_config(shop_id)
    enabled = bool(config and config.get("enabled"))
    fallback = (config or {}).get("manual_fallback_number")
    fallback_normalized = digits_only(fallback)
    salon_existing_normalized = telephony.get("salon_existing_normalized") or ""

    decision = await decide_session(
        shop_id=shop_id, enabled=enabled,
        min_reserve=settings.voice_min_session_reserve_tokens,
    )

    if decision.attach:
        return _dial_sip(shop_id, settings)

    # Detached — pick the safest available fallback
    if fallback and fallback_normalized and fallback_normalized != salon_existing_normalized:
        return _dial_fallback(fallback)

    if fallback and fallback_normalized == salon_existing_normalized:
        logger.warning(
            "twiml.incoming: fallback equals forwarded number — loop risk, "
            "playing Say. shop_id=%s sid=%s", shop_id, call_sid,
        )

    return _say_unavailable()
