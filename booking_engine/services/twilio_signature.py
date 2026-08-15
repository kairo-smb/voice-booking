"""Verify Twilio's X-Twilio-Signature header.

One implementation, shared by the TwiML voice webhook and the SMS webhooks.
Twilio signs the full request URL, so the path must be passed in by the caller
rather than hardcoded.
"""
from __future__ import annotations

from fastapi import Request
from twilio.request_validator import RequestValidator

from booking_engine.config import Settings


def twilio_signature_valid(
    request: Request, form: dict, settings: Settings, *, path: str
) -> bool:
    """Verify X-Twilio-Signature; no-op until TWILIO_AUTH_TOKEN is provisioned."""
    if not settings.twilio_auth_token:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{settings.public_base_url}{path}"
    return RequestValidator(settings.twilio_auth_token).validate(url, form, signature)
