"""Build the OpenAI Realtime session config to accept an inbound SIP call.

OpenAI native SIP: Twilio dials sip:{project};X-Shop-Id=..@sip.api.openai.com,
OpenAI fires a `realtime.call.incoming` webhook, and we POST the session config
(prompt + the 12 authz'd tools) to /v1/realtime/calls/{call_id}/accept.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from booking_engine.services.identity_resolver import ResolutionResult
from booking_engine.services.prompt_assembler import assemble_session_prompt

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


def to_realtime_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap safety-layer tool schemas in OpenAI Realtime function-tool shape."""
    return [
        {
            "type": "function",
            "name": s["name"],
            "description": s.get("description", ""),
            "parameters": s.get("parameters", {"type": "object", "properties": {}}),
        }
        for s in schemas
    ]


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


async def build_accept_payload(
    *,
    config: dict[str, Any],
    policy: dict[str, Any],
    resolution: ResolutionResult,
    model: str,
    allowlist: list[str] | None = None,
    mcp_server_url: str | None = None,
    mcp_token: str | None = None,
) -> dict[str, Any]:
    """Assemble the /accept body: prompt, mapped voice, and tools.

    When `mcp_server_url` is given, tools are served by our remote MCP server
    (OpenAI calls it directly, passing `mcp_token` as the bearer). Otherwise the
    12 function schemas are inlined (fallback / non-MCP path).
    """
    assembled = await assemble_session_prompt(
        config=config, policy=policy, resolution=resolution, allowlist=allowlist,
    )
    if mcp_server_url:
        tools = [{
            "type": "mcp",
            "server_label": "kairo",
            "server_url": mcp_server_url,
            "authorization": mcp_token,
            "require_approval": "never",
            "allowed_tools": [s["name"] for s in assembled.tools],
        }]
    else:
        tools = to_realtime_tools(assembled.tools)
    return {
        "type": "realtime",
        "model": model,
        "instructions": assembled.prompt,
        "voice": _VOICE_MAP.get(assembled.voice, "verse"),
        "tools": tools,
    }
