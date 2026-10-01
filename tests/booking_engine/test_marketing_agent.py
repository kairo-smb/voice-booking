"""The wire contract with marketing-engine's `/whatsapp/agent`.

The payload is `{shop_id, call_id, messages, now}` and nothing else: the engine
loads the shop, the customer and the catalogue itself (the customer-agents
common layer), keyed off the session row `call_id` names.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
import respx

from booking_engine.clients import marketing_agent
from booking_engine.config import Settings

URL = "http://market-intel.test/whatsapp/agent"


@respx.mock
@pytest.mark.asyncio
async def test_posts_only_the_session_and_the_transcript():
    shop, call = uuid4(), uuid4()
    now = datetime(2026, 9, 28, 11, 39, tzinfo=timezone.utc)
    route = respx.post(URL).mock(return_value=httpx.Response(
        200, json={"data": {"text": "Certo!", "escalate": False}}))

    out = await marketing_agent.turn(
        shop_id=shop, call_id=call,
        messages=[{"role": "user", "content": "vorrei prenotare"}],
        now=now,
        settings=Settings(market_intel_api_url="http://market-intel.test",
                          market_intel_secret="test-secret"),
    )

    assert out.text == "Certo!" and out.escalate is False
    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "shop_id": str(shop), "call_id": str(call),
        "messages": [{"role": "user", "content": "vorrei prenotare"}],
        "now": now.isoformat(),
    }
