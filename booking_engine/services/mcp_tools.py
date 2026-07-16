"""MCP tool definitions + executor.

The remote MCP server (mcp_server.py) exposes these to OpenAI. Each call is
gated by the per-call token and proxied to the existing /voice/tools endpoints,
so ALL authorization/constraint logic is reused — no duplication.
"""
from __future__ import annotations

from typing import Any

from httpx import ASGITransport, AsyncClient

from booking_engine.services.call_token import verify_call_token
from booking_engine.services.safety_layer import (
    DEFAULT_TOOL_ALLOWLIST, _TOOL_SCHEMAS,
)

# Tool metadata for MCP tools/list (name, description, inputSchema).
TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": name,
        "description": _TOOL_SCHEMAS[name].get("description", ""),
        "inputSchema": _TOOL_SCHEMAS[name].get(
            "parameters", {"type": "object", "properties": {}}),
    }
    for name in DEFAULT_TOOL_ALLOWLIST
    if name in _TOOL_SCHEMAS
]


async def execute_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    token: str,
    secret: str,
    app=None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Verify the per-call token, then proxy to /voice/tools/{name}.

    Pass `app` for in-process (tests) or `base_url` for a real HTTP hop (prod,
    to the same service). All authz/constraint logic lives in the endpoints.
    """
    if name not in {t["name"] for t in TOOL_DEFS}:
        return {"ok": False, "error": "unknown_tool"}
    claims = verify_call_token(token=token, secret=secret)
    if not claims:
        return {"ok": False, "error": "unauthorized"}
    if app is not None:
        client_kwargs = {"transport": ASGITransport(app=app), "base_url": "http://mcp"}
    else:
        client_kwargs = {"base_url": base_url or ""}
    async with AsyncClient(**client_kwargs) as c:
        r = await c.post(
            f"/voice/tools/{name}",
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Shop-Id": claims["shop_id"],
                "X-Call-Id": claims["call_id"],
            },
            json=arguments or {},
        )
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "error": f"http_{r.status_code}"}
