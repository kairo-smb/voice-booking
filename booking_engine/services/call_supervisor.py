"""Per-call server-side Realtime control WebSocket for SIP calls.

The SIP accept path is fire-and-forget: OpenAI drives the call and, with hosted
MCP tools, does NOT auto-speak tool results or greet first. This worker opens a
control WS to the accepted call (wss://api.openai.com/v1/realtime?call_id=...)
and sends `response.create` to greet on connect and again after each MCP tool
completes, so the agent voices the result. See
docs/superpowers/specs/2026-07-21-sip-call-supervisor-design.md.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SupervisorState:
    response_active: bool = False
    nudge_pending: bool = False
    greeted: bool = False
    tool_started_at: dict[str, float] = field(default_factory=dict)


def decide(event: dict, state: SupervisorState) -> list[dict]:
    """Pure decision core: mutate response/nudge state, return client events to send."""
    etype = event.get("type")
    if etype == "response.created":
        state.response_active = True
        state.nudge_pending = False
        return []
    if etype == "response.done":
        state.response_active = False
        return []
    if etype == "response.output_item.done" and (event.get("item") or {}).get("type") == "mcp_call":
        if not state.response_active and not state.nudge_pending:
            state.nudge_pending = True
            return [{"type": "response.create"}]
    return []


def log_record(call_id: str, event: dict, state: SupervisorState) -> dict:
    """Build one structured log line; track/compute MCP tool latency as a side effect."""
    etype = event.get("type")
    item = event.get("item") or {}
    rec: dict = {"call_id": call_id, "event": etype}
    if etype == "response.output_item.added" and item.get("type") == "mcp_call":
        if item.get("id"):
            state.tool_started_at[item["id"]] = time.monotonic()
    elif etype == "response.output_item.done" and item.get("type") == "mcp_call":
        rec["tool"] = item.get("name")
        started = state.tool_started_at.pop(item.get("id"), None)
        if started is not None:
            rec["latency_ms"] = round((time.monotonic() - started) * 1000)
    return rec
