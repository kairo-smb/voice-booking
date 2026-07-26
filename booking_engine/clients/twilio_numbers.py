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
    """Return up to `limit` available mobile numbers for the country."""
    client = Client(account_sid, auth_token)
    kwargs: dict = {"limit": limit}
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
    """Purchase a number and bind its Voice URL to the dynamic TwiML webhook.

    `bundle_sid` ties the purchase to the one Kairo-entity regulatory Bundle
    (created once, out-of-band) reused across every DID — required for
    regulated number types like Estonia mobile.
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
