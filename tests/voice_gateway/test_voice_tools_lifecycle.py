"""Tests for mark_outcome and escalate_to_merchant tool endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from booking_engine.api.app import create_app

_app = create_app()

AUTH = {"Authorization": "Bearer tool-secret", "X-Shop-Id": str(uuid4()),
        "X-Call-Id": str(uuid4())}


@pytest.fixture(autouse=True)
def stub_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_SECRET", "tool-secret")


@pytest.mark.asyncio
async def test_mark_outcome_updates_call_row():
    with patch("booking_engine.api.routes.voice_tools_lifecycle.set_call_outcome",
               new=AsyncMock(return_value=None)) as fn:
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/mark_outcome",
                headers=AUTH,
                json={"outcome": "booked",
                      "summary": "Maria ha prenotato venerdì alle 10."},
            )
    assert r.json()["ok"] is True
    fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalate_creates_memo_and_pushes():
    memo_id = uuid4()
    customer_id = uuid4()
    insert = AsyncMock(return_value=memo_id)
    # get_call does SELECT * -> real columns are caller_number / customer_id
    with patch("booking_engine.api.routes.voice_tools_lifecycle.insert_callback_memo",
               new=insert), \
         patch("booking_engine.api.routes.voice_tools_lifecycle.send_push",
               new=AsyncMock(return_value=None)) as push, \
         patch("booking_engine.api.routes.voice_tools_lifecycle.set_call_outcome",
               new=AsyncMock(return_value=None)), \
         patch("booking_engine.api.routes.voice_tools_lifecycle.get_call",
               new=AsyncMock(return_value={
                   "shop_id": uuid4(),
                   "customer_id": customer_id,
                   "caller_number": "+393201234567",
               })):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/voice/tools/escalate_to_merchant",
                headers=AUTH,
                json={"reason": "vuole parlare con Giulia",
                      "callback_window": "oggi pomeriggio",
                      "customer_message": "Vorrebbe cambiare data."},
            )
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["memo_id"] == str(memo_id)
    push.assert_awaited_once()
    assert push.await_args.kwargs["event"] == "voice_new_memo"
    # the memo must carry the real caller number + customer link (not null)
    assert insert.await_args.kwargs["caller_phone"] == "+393201234567"
    assert insert.await_args.kwargs["customer_id"] == customer_id
    assert push.await_args.kwargs["payload"]["caller_phone"] == "+393201234567"