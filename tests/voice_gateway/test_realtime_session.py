"""Build the OpenAI Realtime 'accept' payload for an inbound SIP call."""
from __future__ import annotations

import pytest

from booking_engine.services.identity_resolver import ResolutionResult
from booking_engine.services.realtime_session import (
    CUSTOMER_AGENT_TOOLS, build_accept_payload, build_sip_uri, shop_id_from_sip_headers,
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
async def test_accept_payload_appends_agent_rules_after_the_persona():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    payload = await build_accept_payload(
        config=_config(), policy=_policy(), resolution=resolution,
        model="gpt-realtime", agent_instructions="REGOLE_DAL_MOTORE",
    )
    assert payload["type"] == "realtime"
    assert payload["model"] == "gpt-realtime"
    instructions = payload["instructions"]
    assert "Salone Lucia" in instructions
    assert instructions.rstrip().endswith("REGOLE_DAL_MOTORE")
    assert instructions.index("Salone Lucia") < instructions.index("REGOLE_DAL_MOTORE")


@pytest.mark.asyncio
async def test_accept_payload_is_persona_only_without_agent_rules():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    payload = await build_accept_payload(
        config=_config(), policy=_policy(), resolution=resolution,
        model="gpt-realtime",
    )
    assert "Salone Lucia" in payload["instructions"]
    # The old in-repo rule block is gone: the rules come from marketing-engine.
    assert "REGOLE NON NEGOZIABILI" not in payload["instructions"]


@pytest.mark.asyncio
async def test_accept_payload_has_no_tools_without_an_mcp_server():
    # No inline function tools any more: nothing in this repo executes them.
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    payload = await build_accept_payload(
        config=_config(), policy=_policy(), resolution=resolution,
        model="gpt-realtime",
    )
    assert payload["tools"] == []


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
async def test_accept_payload_registers_the_customer_agents_mcp_server():
    resolution = ResolutionResult(is_anonymous=False, matches=[])
    payload = await build_accept_payload(
        config=_config(), policy=_policy(), resolution=resolution,
        model="gpt-realtime",
        mcp_server_url="https://mi/customer-agents/voice/mcp", mcp_token="tok123",
    )
    tools = payload["tools"]
    assert len(tools) == 1
    mcp = tools[0]
    assert mcp["type"] == "mcp"
    assert mcp["server_url"] == "https://mi/customer-agents/voice/mcp"
    assert mcp["authorization"] == "tok123"
    assert mcp["require_approval"] == "never"
    assert mcp["allowed_tools"] == list(CUSTOMER_AGENT_TOOLS)


def test_allowed_tools_are_the_twelve_customer_agent_names():
    # The approved naming table (plan 2026-09-28) plus customer_history
    # (2026-09-29, "il solito"). A name missing here makes OpenAI filter that
    # marketing-engine tool out of every call.
    assert set(CUSTOMER_AGENT_TOOLS) == {
        "customers_identify", "customer_history", "services_catalog", "availability_search",
        "create_customer", "update_customer", "create_appointment",
        "appointments_upcoming", "reschedule_appointment", "cancel_appointment",
        "escalate_to_owner", "set_conversation_outcome",
    }
    assert len(CUSTOMER_AGENT_TOOLS) == 12
