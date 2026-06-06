"""Telnyx Numbers API client — search and purchase IT geographic numbers.

Used only at onboarding time. After purchase, the number's Voice URL
points to our /voice/texml/incoming webhook for dynamic per-call routing.
"""
from __future__ import annotations

from dataclasses import dataclass

from telnyx import Telnyx


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
    api_key: str,
) -> list[AvailableNumber]:
    """Return up to `limit` available numbers for the country and area code."""
    client = Telnyx(api_key=api_key)
    kwargs: dict = {"filter_country_code": country, "limit": limit}
    if area_code:
        kwargs["filter_national_destination_code"] = area_code
    response = client.available_phone_numbers.list(**kwargs)
    return [
        AvailableNumber(
            phone_number=n.phone_number,
            friendly_name=getattr(n, "friendly_name", "") or "",
            locality=getattr(n, "city", "") or "",
            region=(
                n.region_information[0].region_name
                if getattr(n, "region_information", None)
                else ""
            ),
        )
        for n in response.data
    ]


def purchase_number(
    *,
    phone_number: str,
    voice_url: str,
    api_key: str,
) -> PurchasedNumber:
    """Purchase a number and bind its Voice URL to the dynamic TeXML webhook."""
    client = Telnyx(api_key=api_key)
    result = client.number_orders.create(
        phone_numbers=[{"phone_number": phone_number}],
    )
    order = result.data
    purchased_number = order.phone_numbers[0].phone_number
    return PurchasedNumber(sid=order.id, phone_number=purchased_number)
