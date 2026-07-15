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


def ensure_texml_application(*, voice_url: str, api_key: str) -> str:
    """Return the id of the TeXML Application whose voice webhook is `voice_url`.

    One shared application routes every inbound call to our webhook, which
    then looks up the shop by the dialed number. Reused if it already exists,
    created (wired to the account's outbound voice profile for the SIP leg to
    OpenAI) if not.
    """
    client = Telnyx(api_key=api_key)
    for app in client.texml_applications.list().data:
        if getattr(app, "voice_url", None) == voice_url:
            return app.id
    profiles = client.outbound_voice_profiles.list().data
    outbound = {"outbound_voice_profile_id": profiles[0].id} if profiles else None
    created = client.texml_applications.create(
        friendly_name="kairo-voice",
        voice_url=voice_url,
        voice_method="post",
        outbound=outbound,
    )
    return created.data.id


def purchase_number(
    *,
    phone_number: str,
    voice_url: str,
    api_key: str,
    connection_id: str | None = None,
) -> PurchasedNumber:
    """Purchase a number via a Telnyx NumberOrder.

    When `connection_id` (a TeXML Application id) is given, the number is born
    attached to it, so inbound calls route to that app's `voice_url` — no manual
    portal step. `voice_url` is retained for traceability/logging.
    """
    client = Telnyx(api_key=api_key)
    order_body: dict = {"phone_numbers": [{"phone_number": phone_number}]}
    if connection_id:
        order_body["connection_id"] = connection_id
    result = client.number_orders.create(**order_body)
    order = result.data
    purchased_number = order.phone_numbers[0].phone_number
    return PurchasedNumber(sid=order.id, phone_number=purchased_number)
