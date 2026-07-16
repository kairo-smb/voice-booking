"""Build the OpenAI Realtime 'accept' payload for an inbound SIP call."""
from __future__ import annotations

import pytest

from booking_engine.services.identity_resolver import ResolutionResult
from booking_engine.services.realtime_session import (
    build_accept_payload, shop_id_from_sip_headers, to_realtime_tools,
)


def _config(**kw):
    base = {
        "display_name": "Salone Lucia",
        "greeting_after_disclosure": "Sono Aria.",
        "greeting_overflow": "",
        "tone_id": None,
        "voice_preset": "warm_female",
        "answer_mode": "always_on",
    }
    base.update(kw)
    return base


def _policy():
    return {"disclosure_text": "Salve, assistente AI."}


def test_to_realtime_tools_wraps_as_function_type():
    out = to_realtime_tools([
        {"name": "create_booking", "description": "d",
         "parameters": {"type": "object", "properties": {}}},
    ])
    assert out[0]["type"] == "function"
    assert out[0]["name"] == "create_booking"
    assert out[0]["parameters"] == {"type": "object", "properties": {}}


def test_shop_id_from_sip_headers_reads_custom_header():
    headers = [
        {"name": "From", "value": "sip:+393331112222@x"},
        {"name": "X-Shop-Id", "value": "5e0b3ecf-c85f-478f-9369-859c419e7df0"},
    ]
    assert str(shop_id_from_sip_headers(headers)) == \
        "5e0b3ecf-c85f-478f-9369-859c419e7df0"


def test_shop_id_from_sip_headers_none_when_absent():
    assert shop_id_from_sip_headers([{"name": "From", "value": "x"}]) is None


@pytest.mark.asyncio
async def test_accept_payload_has_model_instructions_and_tools():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    payload = await build_accept_payload(
        config=_config(), policy=_policy(), resolution=resolution,
        model="gpt-realtime",
    )
    assert payload["type"] == "realtime"
    assert payload["model"] == "gpt-realtime"
    assert "REGOLE NON NEGOZIABILI" in payload["instructions"]
    names = {t["name"] for t in payload["tools"]}
    assert "create_booking" in names and "escalate_to_merchant" in names
    assert all(t["type"] == "function" for t in payload["tools"])


@pytest.mark.asyncio
async def test_accept_payload_maps_voice_preset_to_openai_voice():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    payload = await build_accept_payload(
        config=_config(voice_preset="neutral_male"), policy=_policy(),
        resolution=resolution, model="gpt-realtime",
    )
    assert payload["voice"] == "ash"  # neutral_male -> ash
