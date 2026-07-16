"""Remote MCP server exposing the voice tools to OpenAI Realtime.

Mounted at /mcp. OpenAI (per the accept payload) calls it with the per-call
bearer token minted by the SIP handler; each tool is proxied to the existing
/voice/tools endpoints so all authorization/constraint logic is reused.

The OpenAI<->MCP transport handshake can only be validated once deployed
(public URL); the tool list + executor are unit-tested in-process.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from booking_engine.config import get_settings
from booking_engine.services.mcp_tools import TOOL_DEFS, execute_tool

_server: Server = Server("kairo")


@_server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=t["name"], description=t["description"],
                   inputSchema=t["inputSchema"])
        for t in TOOL_DEFS
    ]


@_server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
    settings = get_settings()
    token = ""
    try:  # HTTP transport exposes the raw request (with the bearer) here
        token = _server.request_context.request.headers.get("authorization", "")
    except Exception:  # noqa: BLE001 - no request context (e.g. non-HTTP)
        pass
    result = await execute_tool(
        name, arguments, token=token,
        secret=settings.openai_tool_secret,
        base_url=settings.public_base_url,
    )
    return [types.TextContent(type="text", text=json.dumps(result))]


_session_manager = StreamableHTTPSessionManager(
    app=_server, stateless=True, json_response=True,
)


async def mcp_asgi(scope, receive, send) -> None:
    """ASGI entrypoint to mount at /mcp."""
    await _session_manager.handle_request(scope, receive, send)


@asynccontextmanager
async def mcp_lifespan():
    """Run the MCP session manager; enter this from the app lifespan."""
    async with _session_manager.run():
        yield
