"""_call_tool must dispatch in-process (no outbound self-HTTP hop).

Root cause of dead air during tool calls: execute_tool() was invoked with
base_url=settings.public_base_url, making the app call its own public URL
over real HTTPS for every tool — a fresh TCP+TLS handshake each time, for a
route that lives in the same process. create_app() now hands mcp_server a
reference to itself so _call_tool can use the in-process ASGI transport
instead (same path tests for mcp_tools.execute_tool already use).
"""
from __future__ import annotations

import pytest

from booking_engine import mcp_server
from booking_engine.api.app import create_app


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_SECRET", "tool-secret")


def test_create_app_registers_itself_with_mcp_server():
    app = create_app()
    assert mcp_server._app_ref is app


@pytest.mark.asyncio
async def test_call_tool_dispatches_via_app_not_base_url(monkeypatch):
    app = create_app()
    captured = {}

    async def _fake_execute_tool(name, arguments, *, token, secret, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "data": None}

    monkeypatch.setattr(mcp_server, "execute_tool", _fake_execute_tool)
    await mcp_server._call_tool("get_services", {})

    assert captured.get("app") is app
    assert "base_url" not in captured
