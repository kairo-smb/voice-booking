"""MCP tool executor: token-gated proxy to the /voice/tools endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from booking_engine.api.app import create_app
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.mcp_tools import TOOL_DEFS, execute_tool

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
                    "price_cents": 4500}])):
        res = await execute_tool(
            "get_services", {}, token=token, secret=SECRET, app=create_app())
    assert res["ok"] is True
    assert res["data"][0]["name"] == "Colore"
