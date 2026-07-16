"""Thin client for OpenAI Realtime SIP call control."""
from __future__ import annotations

from typing import Any

import httpx

_ACCEPT_URL = "https://api.openai.com/v1/realtime/calls/{call_id}/accept"


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
