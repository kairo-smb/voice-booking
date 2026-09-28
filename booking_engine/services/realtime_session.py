"""Build the OpenAI Realtime session config to accept an inbound SIP call.

OpenAI native SIP: Twilio dials sip:{project}@sip.api.openai.com?X-Shop-Id=..,
OpenAI fires a `realtime.call.incoming` webhook, and we POST the session config
to /v1/realtime/calls/{call_id}/accept: the salon's persona (this repo), the
agent rules and the MCP tool server (both marketing-engine's customer agents,
AGENTS.md 2026-09-28).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from booking_engine.services.identity_resolver import ResolutionResult
from booking_engine.services.prompt_assembler import assemble_session_prompt

# semantic_vad (vs the server_vad default) waits for a modeled turn-end
# probability instead of a fixed silence timeout, and interrupt_response
# lets the caller barge in over the agent mid-response.
_TURN_DETECTION = {
    "type": "semantic_vad",
    "eagerness": "auto",
    "interrupt_response": True,
}

# Our display presets -> OpenAI Realtime voices.
_VOICE_MAP = {
    "alloy": "alloy",
    "ash": "ash",
    "ballad": "ballad",
    "coral": "coral",
    "echo": "echo",
    "sage": "sage",
    "shimmer": "shimmer",
    "verse": "verse",
}


# The customer agents' names (plan 2026-09-28, naming table). OpenAI filters the
# MCP server's `tools/list` by this list, so a stale name here silently removes
# a tool from every call; an unknown one is harmless.
CUSTOMER_AGENT_TOOLS: tuple[str, ...] = (
    "customers_identify",
    "services_catalog",
    "availability_search",
    "create_customer",
    "update_customer",
    "create_appointment",
    "appointments_upcoming",
    "reschedule_appointment",
    "cancel_appointment",
    "escalate_to_owner",
    "set_conversation_outcome",
)


def build_sip_uri(shop_id: object, project_id: str) -> str:
    """The SIP URI OpenAI routes to our incoming-call webhook.

    `X-Shop-Id` is passed via Twilio's documented custom-SIP-header syntax for
    the <Dial><Sip> noun: a query string appended after the host
    (`sip:user@host?X-Header=value`), which Twilio translates into a real
    `X-Shop-Id` SIP header on the INVITE it sends to OpenAI — which is what
    `shop_id_from_sip_headers()` reads back out on the other end. (Previously
    this put the parameter *before* the `@`, which is neither valid bare SIP
    URI syntax nor Twilio's convention — confirmed broken by a raw SIP test
    call: OpenAI echoed back an unparseable `To` header built from it.)
    """
    return f"sip:{project_id}@sip.api.openai.com?X-Shop-Id={shop_id}"


def shop_id_from_sip_headers(sip_headers: list[dict[str, str]]) -> UUID | None:
    """Read our X-Shop-Id custom header from the incoming-call SIP headers."""
    for h in sip_headers or []:
        if str(h.get("name", "")).lower() == "x-shop-id":
            try:
                return UUID(str(h.get("value", "")).strip())
            except (ValueError, AttributeError):
                return None
    return None


def caller_from_sip_headers(sip_headers: list[dict[str, str]]) -> str | None:
    """Extract the caller number from the From header (sip:+39...@host)."""
    for h in sip_headers or []:
        if str(h.get("name", "")).lower() == "from":
            val = str(h.get("value", ""))
            # sip:+393331112222@host -> +393331112222
            core = val.split(":", 1)[-1].split("@", 1)[0]
            return core or None
    return None


_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"


async def build_accept_payload(
    *,
    config: dict[str, Any],
    policy: dict[str, Any],
    resolution: ResolutionResult,
    model: str,
    mcp_server_url: str | None = None,
    mcp_token: str | None = None,
    agent_instructions: str | None = None,
    enable_input_transcription: bool = False,
) -> dict[str, Any]:
    """Assemble the /accept body: persona + agent rules, mapped voice, tools.

    `agent_instructions` (marketing-engine's rules for this call) go after the
    persona; None leaves the persona alone. The tools are marketing-engine's
    MCP server, which OpenAI calls directly with `mcp_token` as the bearer —
    and nothing when there is no server: this repo no longer executes any tool
    itself, so inlining function schemas would give the model tools nobody
    answers.

    `enable_input_transcription` asks OpenAI to also transcribe the caller's
    speech (off by default — debug/test use only, see
    `call_supervisor_verbose_logging`; real calls don't need a server-side
    text transcript of the customer for anything today).
    """
    assembled = await assemble_session_prompt(
        config=config, policy=policy, resolution=resolution,
    )
    instructions = assembled.prompt
    if agent_instructions:
        instructions = f"{instructions}\n\n{agent_instructions}"
    tools: list[dict[str, Any]] = []
    if mcp_server_url:
        tools = [{
            "type": "mcp",
            "server_label": "kairo",
            "server_url": mcp_server_url,
            "authorization": mcp_token,
            "require_approval": "never",
            "allowed_tools": list(CUSTOMER_AGENT_TOOLS),
        }]
    audio_input: dict[str, Any] = {"turn_detection": _TURN_DETECTION}
    if enable_input_transcription:
        audio_input["transcription"] = {"model": _TRANSCRIPTION_MODEL}
    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "audio": {
            "input": audio_input,
            "output": {"voice": _VOICE_MAP.get(assembled.voice, "verse")},
        },
        "tools": tools,
    }
