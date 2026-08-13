"""Tests for the Twilio Regulatory Compliance API client.

Estonia mobile is a `business`-only regulation with exactly one End-User
field (`business_name`) and no address/VAT/personal-ID field — these tests
exist specifically to stop someone "helpfully" adding those fields back in,
since sending attributes a regulation doesn't request is a known cause of
evaluation failure (see booking_engine/clients/twilio_regulatory.py).
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from booking_engine.clients.twilio_regulatory import (
    Violation,
    create_end_user,
    evaluate,
    get_bundle_status,
    get_regulation_sid,
)

_BASE = "https://numbers.twilio.com/v2/RegulatoryCompliance"


@pytest.mark.asyncio
@respx.mock
async def test_get_regulation_sid_returns_sid_from_first_result():
    respx.get(f"{_BASE}/Regulations").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"sid": "RN26dca8d0e541a6c8fce4abd46e518506",
                 "friendly_name": "Estonia: Mobile - Business"},
            ],
        })
    )
    sid = await get_regulation_sid(
        iso_country="EE", number_type="mobile",
        account_sid="AC", auth_token="tok",
    )
    assert sid == "RN26dca8d0e541a6c8fce4abd46e518506"


@pytest.mark.asyncio
@respx.mock
async def test_get_regulation_sid_returns_none_for_empty_results():
    """A country with no matching regulation must not crash the caller."""
    respx.get(f"{_BASE}/Regulations").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    sid = await get_regulation_sid(
        iso_country="XX", number_type="mobile",
        account_sid="AC", auth_token="tok",
    )
    assert sid is None


@pytest.mark.asyncio
@respx.mock
async def test_create_end_user_sends_type_business_and_only_business_name():
    route = respx.post(f"{_BASE}/EndUsers").mock(
        return_value=httpx.Response(201, json={"sid": "IT123"})
    )
    sid = await create_end_user(
        business_name="Salone Bella", account_sid="AC", auth_token="tok",
    )
    assert sid == "IT123"

    request = route.calls.last.request
    sent = dict(httpx.QueryParams(request.content.decode()))
    assert sent["Type"] == "business"

    attrs = json.loads(sent["Attributes"])
    # Exactly one key — no address/VAT/personal-ID smuggled in.
    assert attrs == {"business_name": "Salone Bella"}
    assert list(attrs.keys()) == ["business_name"]


@pytest.mark.asyncio
@respx.mock
async def test_evaluate_returns_compliant_true_and_no_violations():
    respx.post(f"{_BASE}/Bundles/BU1/Evaluations").mock(
        return_value=httpx.Response(200, json={"status": "compliant", "results": []})
    )
    ok, violations = await evaluate(bundle_sid="BU1", account_sid="AC", auth_token="tok")
    assert ok is True
    assert violations == []


@pytest.mark.asyncio
@respx.mock
async def test_evaluate_maps_only_failed_entries_when_noncompliant():
    respx.post(f"{_BASE}/Bundles/BU1/Evaluations").mock(
        return_value=httpx.Response(200, json={
            "status": "noncompliant",
            "results": [
                {"friendly_name": "business_name", "description": "is required",
                 "passed": False},
                {"friendly_name": "commercial_registrar_excerpt",
                 "description": "looks fine", "passed": True},
            ],
        })
    )
    ok, violations = await evaluate(bundle_sid="BU1", account_sid="AC", auth_token="tok")
    assert ok is False
    assert violations == [
        Violation(friendly_name="business_name", description="is required"),
    ]


@pytest.mark.asyncio
@respx.mock
async def test_get_bundle_status_returns_raw_status_string():
    respx.get(f"{_BASE}/Bundles/BU1").mock(
        return_value=httpx.Response(200, json={"status": "twilio-approved"})
    )
    status = await get_bundle_status(
        bundle_sid="BU1", account_sid="AC", auth_token="tok",
    )
    assert status == "twilio-approved"
