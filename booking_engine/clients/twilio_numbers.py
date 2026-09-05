"""Twilio Numbers API client — search and purchase EU mobile numbers.

Used only at onboarding time. After purchase, the number's Voice URL
points to our /voice/twiml/incoming webhook for dynamic per-call routing.
"""
from __future__ import annotations

from dataclasses import dataclass

from twilio.rest import Client


@dataclass
class AvailableNumber:
    phone_number: str
    friendly_name: str
    locality: str
    region: str


@dataclass
class PurchasedNumber:
    sid: str
    phone_number: str


def search_available_numbers(
    *,
    area_code: str | None,
    country: str,
    limit: int,
    account_sid: str,
    auth_token: str,
) -> list[AvailableNumber]:
    """Return up to `limit` available SMS-capable mobile numbers for the country.

    `sms_enabled=True` is a hard requirement, not a preference: the number is
    the salon's SMS sender, and a voice-only one would provision cleanly and
    then fail on the first send. Estonian mobiles happen to be SMS-capable
    today, which is exactly why this is filtered rather than assumed — a
    catalogue change would otherwise surface as a broken salon, not an error.
    """
    client = Client(account_sid, auth_token)
    kwargs: dict = {"limit": limit, "sms_enabled": True}
    if area_code:
        kwargs["area_code"] = area_code
    found = client.available_phone_numbers(country).mobile.list(**kwargs)
    return [
        AvailableNumber(
            phone_number=n.phone_number,
            friendly_name=n.friendly_name or "",
            locality=n.locality or "",
            region=n.region or "",
        )
        for n in found
    ]


def purchase_number(
    *,
    phone_number: str,
    voice_url: str,
    account_sid: str,
    auth_token: str,
    bundle_sid: str | None = None,
    address_sid: str | None = None,
) -> PurchasedNumber:
    """Purchase a number and bind its voice webhook.

    `bundle_sid` ties the purchase to a regulatory Bundle — required for
    regulated number types like Estonia mobile. For self-service provisioning
    this is the *salon's own* bundle, not a shared one (Twilio's ISV rules
    forbid reusing our business info in customer bundles).

    No `sms_url` is set: there is no inbound SMS handler (STOP handling was
    removed — opt-out is now in-store, via customers.marketing_consent; see
    CLAUDE.md). Leaving the number's SMS webhook unset is intentional, not a
    gap.
    """
    client = Client(account_sid, auth_token)
    kwargs: dict = {
        "phone_number": phone_number,
        "voice_url": voice_url,
        "voice_method": "POST",
    }
    if bundle_sid:
        kwargs["bundle_sid"] = bundle_sid
    if address_sid:
        kwargs["address_sid"] = address_sid
    result = client.incoming_phone_numbers.create(**kwargs)
    return PurchasedNumber(sid=result.sid, phone_number=result.phone_number)


def release_number(*, sid: str, account_sid: str, auth_token: str) -> None:
    """Give a number back to Twilio.

    Called when we bought one we cannot store — without this, losing the
    insert race leaks a number billed at ~$3/mo forever.
    """
    Client(account_sid, auth_token).incoming_phone_numbers(sid).delete()


@dataclass
class NumberStatus:
    sid: str
    phone_number: str
    voice_url: str
    sms_url: str


def fetch_number(*, sid: str, account_sid: str, auth_token: str) -> NumberStatus:
    """Read a number back from Twilio.

    Raises TwilioRestException with status 404 if the number no longer belongs
    to this account — which is exactly the signal the health check needs.
    """
    n = Client(account_sid, auth_token).incoming_phone_numbers(sid).fetch()
    return NumberStatus(
        sid=n.sid, phone_number=n.phone_number,
        voice_url=n.voice_url or "", sms_url=n.sms_url or "",
    )
