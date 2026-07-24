"""MCP tool executor: token-gated proxy to the /voice/tools endpoints."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from booking_engine.api.app import create_app
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.mcp_tools import (
    MIN_CHECK_LATENCY_SECONDS, TOOL_DEFS, execute_tool,
)

SECRET = "tool-secret"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_SECRET", SECRET)


def test_tool_defs_expose_the_twelve_tools():
    names = {t["name"] for t in TOOL_DEFS}
    assert "create_booking" in names and "escalate_to_merchant" in names
    assert len(TOOL_DEFS) == 12
    assert all("inputSchema" in t and "description" in t for t in TOOL_DEFS)


@pytest.mark.asyncio
async def test_execute_tool_rejects_invalid_token():
    res = await execute_tool(
        "get_services", {}, token="garbage", secret=SECRET, app=create_app())
    assert res["ok"] is False
    assert res["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_execute_tool_proxies_with_call_context():
    shop, call = uuid4(), uuid4()
    token = mint_call_token(shop_id=shop, call_id=call, secret=SECRET)
    with patch("booking_engine.api.routes.voice_tools_catalog.list_services",
               new=AsyncMock(return_value=[
                   {"id": uuid4(), "name": "Colore", "duration_min": 60,
                    "price_cents": 4500}])), \
         patch("booking_engine.services.mcp_tools.asyncio.sleep", new=AsyncMock()):
        res = await execute_tool(
            "get_services", {}, token=token, secret=SECRET, app=create_app())
    assert res["ok"] is True
    assert res["data"][0]["name"] == "Colore"


@pytest.mark.asyncio
async def test_execute_tool_pads_fast_check_tool_to_min_latency():
    """get_services is an ATTESA tool — a fast mocked response should still
    get padded up to MIN_CHECK_LATENCY_SECONDS, not returned instantly."""
    shop, call = uuid4(), uuid4()
    token = mint_call_token(shop_id=shop, call_id=call, secret=SECRET)
    with patch("booking_engine.api.routes.voice_tools_catalog.list_services",
               new=AsyncMock(return_value=[])), \
         patch("booking_engine.services.mcp_tools.asyncio.sleep",
               new=AsyncMock()) as sleep_mock:
        await execute_tool(
            "get_services", {}, token=token, secret=SECRET, app=create_app())
    sleep_mock.assert_awaited_once()
    (delay,), _ = sleep_mock.call_args
    assert 0 < delay <= MIN_CHECK_LATENCY_SECONDS


@pytest.mark.asyncio
async def test_execute_tool_times_out_cleanly_on_stuck_downstream_call(monkeypatch):
    """In-process dispatch (ASGITransport) has no built-in timeout, unlike the
    real-HTTP path it replaced — a hung downstream call (e.g. DB pool
    exhaustion under concurrent load) must fail cleanly, not hang the tool
    call, and the phone call, forever."""
    monkeypatch.setattr(
        "booking_engine.services.mcp_tools.TOOL_CALL_TIMEOUT_SECONDS", 0.05)
    shop, call = uuid4(), uuid4()
    token = mint_call_token(shop_id=shop, call_id=call, secret=SECRET)

    async def _hang(*args, **kwargs):
        await asyncio.sleep(10)
        return []

    with patch("booking_engine.api.routes.voice_tools_catalog.list_services",
               new=AsyncMock(side_effect=_hang)):
        res = await execute_tool(
            "get_services", {}, token=token, secret=SECRET, app=create_app())
    assert res == {"ok": False, "error": "tool_timeout"}


@pytest.mark.asyncio
async def test_execute_tool_does_not_pad_write_tools():
    """mark_outcome has no ATTESA filler phrase before it — no padding."""
    shop, call = uuid4(), uuid4()
    token = mint_call_token(shop_id=shop, call_id=call, secret=SECRET)
    with patch("booking_engine.api.routes.voice_tools_lifecycle.set_call_outcome",
               new=AsyncMock(return_value=None)), \
         patch("booking_engine.services.mcp_tools.asyncio.sleep",
               new=AsyncMock()) as sleep_mock:
        res = await execute_tool(
            "mark_outcome", {"outcome": "info", "summary": "test"},
            token=token, secret=SECRET, app=create_app())
    assert res["ok"] is True
    sleep_mock.assert_not_awaited()
