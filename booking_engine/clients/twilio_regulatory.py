"""Twilio Regulatory Compliance API — one bundle per salon.

Twilio's ISV rules require a bundle per end customer: "Do not reuse your
business information in customer bundles. Twilio audits this." So each salon
gets its own End-User + supporting document + bundle.

Requirements are queried, never hardcoded — Twilio's docs are explicit that
regulations change and must be read from the Regulations resource. And Twilio
is the validator: Evaluations is synchronous and returns field-level
violations, which we surface verbatim rather than reimplementing Estonian
rules that can change without notice.

See docs/number-provisioning-design.md §2.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from httpx import AsyncClient, BasicAuth

_BASE = "https://numbers.twilio.com/v2/RegulatoryCompliance"
_TIMEOUT = 30.0


@dataclass(frozen=True)
class Violation:
    friendly_name: str
    description: str


def _auth(account_sid: str, auth_token: str) -> BasicAuth:
    return BasicAuth(account_sid, auth_token)


async def _post(path: str, data: dict, *, account_sid: str, auth_token: str,
                files: dict | None = None) -> dict:
    async with AsyncClient(auth=_auth(account_sid, auth_token), timeout=_TIMEOUT) as c:
        r = await c.post(f"{_BASE}{path}", data=data, files=files)
        r.raise_for_status()
        return r.json()


async def _get(path: str, params: dict | None = None, *,
               account_sid: str, auth_token: str) -> dict:
    async with AsyncClient(auth=_auth(account_sid, auth_token), timeout=_TIMEOUT) as c:
        r = await c.get(f"{_BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def get_regulation_sid(*, iso_country: str, number_type: str,
                             account_sid: str, auth_token: str) -> str | None:
    """The regulation for a country + number type, or None if none applies."""
    body = await _get("/Regulations",
                      {"IsoCountry": iso_country, "NumberType": number_type},
                      account_sid=account_sid, auth_token=auth_token)
    results = body.get("results", [])
    return results[0]["sid"] if results else None


async def create_end_user(*, business_name: str,
                          account_sid: str, auth_token: str) -> str:
    """Estonia's regulation asks for exactly one field.

    Do NOT add address/VAT/registration number here — sending attributes a
    regulation does not request is a known cause of evaluation failure.
    """
    body = await _post("/EndUsers", {
        "FriendlyName": business_name,
        "Type": "business",
        "Attributes": json.dumps({"business_name": business_name}),
    }, account_sid=account_sid, auth_token=auth_token)
    return body["sid"]


async def upload_document(*, business_name: str, doc_type: str, filename: str,
                          content: bytes, content_type: str,
                          account_sid: str, auth_token: str) -> str:
    body = await _post("/SupportingDocuments", {
        "FriendlyName": f"{business_name} - {doc_type}",
        "Type": doc_type,
        "Attributes": json.dumps({"business_name": business_name}),
    }, files={"File": (filename, content, content_type)},
       account_sid=account_sid, auth_token=auth_token)
    return body["sid"]


async def create_bundle(*, regulation_sid: str, iso_country: str, email: str,
                        friendly_name: str, account_sid: str, auth_token: str) -> str:
    body = await _post("/Bundles", {
        "FriendlyName": friendly_name,
        "RegulationSid": regulation_sid,
        "IsoCountry": iso_country,
        "EndUserType": "business",
        "Email": email,
    }, account_sid=account_sid, auth_token=auth_token)
    return body["sid"]


async def assign_item(*, bundle_sid: str, object_sid: str,
                      account_sid: str, auth_token: str) -> None:
    await _post(f"/Bundles/{bundle_sid}/ItemAssignments", {"ObjectSid": object_sid},
                account_sid=account_sid, auth_token=auth_token)


async def evaluate(*, bundle_sid: str, account_sid: str,
                   auth_token: str) -> tuple[bool, list[Violation]]:
    """Synchronous. Returns (compliant, violations) with Twilio's own wording."""
    body = await _post(f"/Bundles/{bundle_sid}/Evaluations", {},
                       account_sid=account_sid, auth_token=auth_token)
    if body.get("status") == "compliant":
        return True, []
    return False, [
        Violation(friendly_name=v.get("friendly_name", ""),
                  description=v.get("description", ""))
        for v in body.get("results", [])
        if v.get("passed") is False
    ]


async def submit_for_review(*, bundle_sid: str,
                            account_sid: str, auth_token: str) -> None:
    await _post(f"/Bundles/{bundle_sid}", {"Status": "pending-review"},
                account_sid=account_sid, auth_token=auth_token)


async def get_bundle_status(*, bundle_sid: str,
                            account_sid: str, auth_token: str) -> str:
    body = await _get(f"/Bundles/{bundle_sid}",
                      account_sid=account_sid, auth_token=auth_token)
    return body.get("status", "")
