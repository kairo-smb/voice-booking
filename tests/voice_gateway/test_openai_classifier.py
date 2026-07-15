"""Tests for the OpenAI outcome classifier wrapper."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from voice_gateway.clients.openai_classifier import classify_call


def _make_resp(status: int, body: dict):
    """Build a stand-in for httpx.Response with the methods classify_call uses."""
    class R:
        status_code = status
        def json(self):
            return body
        def raise_for_status(self):
            pass
    return R()


def _patch_httpx_client(post_return):
    """Returns a patcher for httpx.AsyncClient whose post() returns post_return."""
    fake = AsyncMock()
    fake.post = AsyncMock(return_value=post_return)
    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake
    fake_cm.__aexit__.return_value = False
    return patch("voice_gateway.clients.openai_classifier.httpx.AsyncClient",
                 return_value=fake_cm)


@pytest.mark.asyncio
async def test_classify_call_parses_json_response():
    body = {"choices": [{"message": {"content": json.dumps({
        "outcome": "booked",
        "outcome_reason": "Cliente ha prenotato taglio venerdi",
        "summary": "Maria ha prenotato taglio venerdi alle 10:00",
    })}}]}
    with _patch_httpx_client(_make_resp(200, body)):
        result = await classify_call(
            api_key="sk-test", model="gpt-4o-mini",
            transcript=[{"role": "assistant", "text": "Ciao!"},
                        {"role": "caller", "text": "Vorrei prenotare"}],
            booked_appointment_id="abc",
        )
    assert result["outcome"] == "booked"
    assert "Maria" in result["summary"]


@pytest.mark.asyncio
async def test_classify_call_invalid_response_returns_failed():
    body = {"choices": [{"message": {"content": "not json"}}]}
    with _patch_httpx_client(_make_resp(200, body)):
        result = await classify_call(
            api_key="sk-test", model="gpt-4o-mini",
            transcript=[], booked_appointment_id=None,
        )
    assert result["outcome"] == "failed"
    assert result["outcome_reason"] == "classification_invalid"


@pytest.mark.asyncio
async def test_classify_call_unknown_outcome_normalized_to_failed():
    body = {"choices": [{"message": {"content": json.dumps({
        "outcome": "definitely-not-real",
        "outcome_reason": "x", "summary": "x",
    })}}]}
    with _patch_httpx_client(_make_resp(200, body)):
        result = await classify_call(
            api_key="sk-test", model="gpt-4o-mini",
            transcript=[], booked_appointment_id=None,
        )
    assert result["outcome"] == "failed"


@pytest.mark.asyncio
async def test_classify_call_http_error_returns_failed():
    with _patch_httpx_client(_make_resp(500, {})):
        result = await classify_call(
            api_key="sk-test", model="gpt-4o-mini",
            transcript=[], booked_appointment_id=None,
        )
    assert result["outcome"] == "failed"
    assert "500" in result["outcome_reason"]


@pytest.mark.asyncio
async def test_classify_call_includes_service_brief():
    brief = {
        "services_requested": [{"servizio": "colore", "note": "vuole più freddo"}],
        "desired_result": "biondo più freddo",
        "hair_details": {"storia_chimica": "decolorazione 3 mesi fa"},
    }
    body = {"choices": [{"message": {"content": json.dumps({
        "outcome": "escalated", "outcome_reason": "x", "summary": "y",
        "service_brief": brief,
    })}}]}
    with _patch_httpx_client(_make_resp(200, body)):
        result = await classify_call(
            api_key="sk-test", model="gpt-4o-mini",
            transcript=[{"role": "caller", "text": "vorrei schiarire"}],
            booked_appointment_id=None,
        )
    sb = result["service_brief"]
    assert sb["services_requested"][0]["servizio"] == "colore"
    assert sb["desired_result"] == "biondo più freddo"
    assert sb["hair_details"]["storia_chimica"] == "decolorazione 3 mesi fa"


@pytest.mark.asyncio
async def test_classify_call_defaults_empty_service_brief_when_absent():
    body = {"choices": [{"message": {"content": json.dumps({
        "outcome": "info", "outcome_reason": "x", "summary": "y",
    })}}]}
    with _patch_httpx_client(_make_resp(200, body)):
        result = await classify_call(
            api_key="sk-test", model="gpt-4o-mini",
            transcript=[], booked_appointment_id=None,
        )
    assert result["service_brief"]["services_requested"] == []


@pytest.mark.asyncio
async def test_failed_paths_still_carry_empty_service_brief():
    with _patch_httpx_client(_make_resp(500, {})):
        result = await classify_call(
            api_key="sk-test", model="gpt-4o-mini",
            transcript=[], booked_appointment_id=None,
        )
    assert result["service_brief"]["services_requested"] == []
