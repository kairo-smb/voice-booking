"""Thin client for OpenAI Realtime SIP call control."""
from __future__ import annotations

from typing import Any

import httpx

_ACCEPT_URL = "https://api.openai.com/v1/realtime/calls/{call_id}/accept"
_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"


async def accept_sip_call(
    *, call_id: str, payload: dict[str, Any], api_key: str,
) -> bool:
    """Accept an incoming SIP call with the given session config. True on 2xx."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _ACCEPT_URL.format(call_id=call_id),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=payload,
        )
    return 200 <= resp.status_code < 300


async def create_ephemeral_session(
    *, session_config: dict[str, Any], api_key: str,
) -> dict[str, Any]:
    """Mint an ephemeral client secret for a browser WebRTC Realtime session.

    `session_config` is the same session-config dict `build_accept_payload()`
    produces (type/model/instructions/voice/tools). Raises
    `httpx.HTTPStatusError` on a non-2xx response.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _CLIENT_SECRETS_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"session": session_config},
        )
        resp.raise_for_status()
    return resp.json()
