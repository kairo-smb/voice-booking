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
    request: Request, form: dict, settings: Settings, *, path: str,
    auth_token: str | None = None,
) -> bool:
    """Verify X-Twilio-Signature; no-op until TWILIO_AUTH_TOKEN is provisioned.

    `auth_token` overrides the account token. Twilio signs with the token of
    the account that owns the resource, so webhooks about a subaccount's
    WhatsApp traffic are signed with *that subaccount's* token, not ours —
    validating those against `settings.twilio_auth_token` would reject every
    genuine request.
    """
    token = auth_token or settings.twilio_auth_token
    if not token:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{settings.public_base_url}{path}"
    return RequestValidator(token).validate(url, form, signature)
