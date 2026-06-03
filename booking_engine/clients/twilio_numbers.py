"""Twilio Numbers API client — search and purchase IT geographic numbers.

Used only at onboarding time (Path 1). After purchase, the number's Voice URL
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
    """Return up to `limit` available numbers for the country and area code."""
    client = Client(account_sid, auth_token)
    kwargs = {"limit": limit}
    if area_code:
        kwargs["area_code"] = area_code
    found = client.available_phone_numbers(country).local.list(**kwargs)
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
) -> PurchasedNumber:
    """Purchase a number and bind its Voice URL to the dynamic TwiML webhook."""
    client = Client(account_sid, auth_token)
    result = client.incoming_phone_numbers.create(
        phone_number=phone_number,
        voice_url=voice_url,
        voice_method="POST",
    )
    return PurchasedNumber(sid=result.sid, phone_number=result.phone_number)
