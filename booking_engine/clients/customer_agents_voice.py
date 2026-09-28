"""The voice surface of marketing-engine's customer agents.

A phone call's tools and agent rules are not in this repo any more. OpenAI
Realtime calls marketing-engine's MCP endpoint (`/customer-agents/voice/mcp`)
directly, and the rules the model follows come from
`/customer-agents/voice/instructions`: the same common layer, schemas and
`runTool` telemetry the WhatsApp agent runs on (AGENTS.md, 2026-09-28).

Both are authorized with the per-call token this repo mints at accept time
(`services/call_token.py`): it names our `voice_agent.calls` row, which is
where marketing-engine reads the caller number every write is authorized on.

What this repo still owns is the persona — voice, tone, greeting, overflow
text — because that is the salon's configuration, not agent logic.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx

from booking_engine.config import Settings

logger = logging.getLogger(__name__)

_MCP_PATH = "/customer-agents/voice/mcp"
_INSTRUCTIONS_PATH = "/customer-agents/voice/instructions"

# OpenAI's incoming-call webhook is waiting on this: the caller hears ringing
# until we accept. A slow engine costs the rules, never the call.
_TIMEOUT_SECONDS = 5.0


def _base(settings: Settings) -> str:
    return (settings.market_intel_api_url or "").rstrip("/")


def mcp_url(settings: Settings) -> str | None:
    """The MCP server_url for the realtime session, or None when unconfigured.

    No trailing slash needed: Express matches `/mcp` and `/mcp/` alike, with
    no 307 for OpenAI's MCP client to fail to follow (AGENTS.md, 2026-07-21).
    """
    base = _base(settings)
    return f"{base}{_MCP_PATH}" if base else None


async def fetch_instructions(
    *, shop_id: UUID, call_id: UUID, token: str, settings: Settings,
) -> str | None:
    """The agent rules for this call, or None. Never raises.

    Every failure is `logger.error` (an event in GlitchTip): a call answered
    with the persona only will greet and talk, but it has no rules for the
    tools, which is worth someone looking at — and still better than a call
    that rings out.
    """
    base = _base(settings)
    if not base:
        logger.error("voice.instructions_unconfigured shop=%s call=%s: "
                     "MARKET_INTEL_API_URL not set", shop_id, call_id)
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{base}{_INSTRUCTIONS_PATH}",
                params={"shop_id": str(shop_id), "call_id": str(call_id)},
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        logger.error("voice.instructions_unreachable shop=%s call=%s err=%s",
                     shop_id, call_id, exc)
        return None
    if resp.status_code != 200:
        logger.error("voice.instructions_http_%s shop=%s call=%s",
                     resp.status_code, shop_id, call_id)
        return None
    try:
        instructions = resp.json().get("instructions")
    except (ValueError, AttributeError):
        instructions = None
    if not isinstance(instructions, str) or not instructions.strip():
        logger.error("voice.instructions_malformed shop=%s call=%s",
                     shop_id, call_id)
        return None
    return instructions
