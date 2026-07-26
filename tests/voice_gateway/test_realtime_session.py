"""Build the OpenAI Realtime 'accept' payload for an inbound SIP call."""
from __future__ import annotations

import pytest

from booking_engine.services.identity_resolver import ResolutionResult
from booking_engine.services.realtime_session import (
    build_accept_payload, build_sip_uri, shop_id_from_sip_headers, to_realtime_tools,
)


def _config(**kw):
    base = {
        "display_name": "Salone Lucia",
        "greeting_after_disclosure": "Sono Aria.",
        "greeting_overflow": "",
        "tone_id": None,
        "voice_preset": "verse",
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


def test_build_sip_uri_uses_twilio_custom_header_query_syntax():
    # Twilio's documented <Dial><Sip> convention for custom SIP headers is a
    # query string after the host, which Twilio translates into a real
    # X-Shop-Id header on the INVITE it sends OpenAI. Params before "@" are
    # neither valid bare SIP URI syntax nor what Twilio parses.
    shop_id = "5e0b3ecf-c85f-478f-9369-859c419e7df0"
    uri = build_sip_uri(shop_id, "proj_abc")
    assert uri == "sip:proj_abc@sip.api.openai.com?X-Shop-Id=5e0b3ecf-c85f-478f-9369-859c419e7df0"


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
        config=_config(voice_preset="ash"), policy=_policy(),
        resolution=resolution, model="gpt-realtime",
    )
    assert payload["audio"]["output"]["voice"] == "ash"


@pytest.mark.asyncio
async def test_accept_payload_sets_semantic_vad_with_interrupt_response():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    payload = await build_accept_payload(
        config=_config(), policy=_policy(), resolution=resolution,
        model="gpt-realtime",
    )
    turn_detection = payload["audio"]["input"]["turn_detection"]
    assert turn_detection["type"] == "semantic_vad"
    assert turn_detection["interrupt_response"] is True


@pytest.mark.asyncio
async def test_accept_payload_omits_input_transcription_by_default():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    payload = await build_accept_payload(
        config=_config(), policy=_policy(), resolution=resolution,
        model="gpt-realtime",
    )
    assert "transcription" not in payload["audio"]["input"]


@pytest.mark.asyncio
async def test_accept_payload_adds_input_transcription_when_enabled():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    payload = await build_accept_payload(
        config=_config(), policy=_policy(), resolution=resolution,
        model="gpt-realtime", enable_input_transcription=True,
    )
    assert payload["audio"]["input"]["transcription"]["model"]


@pytest.mark.asyncio
async def test_accept_payload_registers_mcp_server_when_url_given():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    payload = await build_accept_payload(
        config=_config(), policy=_policy(), resolution=resolution,
        model="gpt-realtime",
        mcp_server_url="https://x/mcp", mcp_token="tok123",
    )
    tools = payload["tools"]
    assert len(tools) == 1
    mcp = tools[0]
    assert mcp["type"] == "mcp"
    assert mcp["server_url"] == "https://x/mcp"
    assert mcp["authorization"] == "tok123"
    assert mcp["require_approval"] == "never"
    assert "create_booking" in mcp["allowed_tools"]
