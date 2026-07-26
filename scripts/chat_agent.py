"""Talk to the voice agent locally (no phone, no deploy).

Connects to OpenAI Realtime over WebSocket with the SAME session config the SIP
handler would send (prompt + inline function tools), simulates a caller, and
executes the model's tool calls against our real endpoints (Neon data).

Write tools are STUBBED by default so a test run never mutates the shared DB;
pass --live to actually execute them (use a Neon branch for that).

Usage:
    export PYTHONPATH=. ; set -a; source .env; set +a
    python scripts/chat_agent.py <shop_id> <caller> "message one" "message two"
    # interactive:
    python scripts/chat_agent.py <shop_id> <caller>
"""
from __future__ import annotations

import asyncio
import json
import sys
from uuid import UUID

import websockets
from httpx import ASGITransport, AsyncClient

from booking_engine.api.app import create_app
from booking_engine.config import Settings
from booking_engine.db import connection
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.db.voice_config_queries import get_config, get_policy
from booking_engine.services.identity_resolver import resolve_caller
from booking_engine.services.realtime_session import build_accept_payload

_WRITE_TOOLS = {
    "create_booking", "modify_booking", "cancel_booking",
    "create_customer_from_call", "update_customer_from_call",
}


class ToolRunner:
    def __init__(self, *, shop_id, call_id, secret, live):
        self._app = create_app()
        self.shop_id, self.call_id, self.secret, self.live = shop_id, call_id, secret, live

    async def run(self, name: str, args: dict) -> dict:
        if name in _WRITE_TOOLS and not self.live:
            return {"ok": True, "data": {"stubbed": True,
                    "note": "write tool skipped (--live to execute)"}}
        transport = ASGITransport(app=self._app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                f"/voice/tools/{name}",
                headers={"Authorization": f"Bearer {self.secret}",
                         "X-Shop-Id": str(self.shop_id),
                         "X-Call-Id": str(self.call_id)},
                json=args,
            )
        try:
            return r.json()
        except Exception:
            return {"ok": False, "error": f"http_{r.status_code}"}


async def _run_response(ws, runner: ToolRunner) -> None:
    """Drive one response cycle, resolving any tool calls, until it settles."""
    await ws.send(json.dumps({"type": "response.create"}))
    while True:
        text, calls = "", []
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=45)
            except asyncio.TimeoutError:
                print("  !! timed out waiting for response"); return
            ev = json.loads(raw)
            t = ev.get("type", "")
            if t.endswith("output_text.delta") or t.endswith("text.delta") \
                    or t == "response.audio_transcript.delta":
                text += ev.get("delta", "")
            elif t == "response.function_call_arguments.done":
                calls.append((ev.get("name"), ev.get("call_id"), ev.get("arguments", "{}")))
            elif t == "response.done":
                break
            elif t == "error":
                print("  !! OpenAI error:", json.dumps(ev.get("error", ev))[:300])
                return
        if text.strip():
            print("AGENT:", text.strip())
        if not calls:
            return
        for name, call_id, args_json in calls:
            args = json.loads(args_json or "{}")
            result = await runner.run(name, args)
            print(f"  [tool {name}({args_json}) -> {json.dumps(result)[:140]}]")
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call_id,
                "output": json.dumps(result),
            }}))
        await ws.send(json.dumps({"type": "response.create"}))


async def chat(shop_id: UUID, caller: str, messages: list[str], live: bool) -> None:
    settings = Settings()
    await connection.init_connection(settings)
    try:
        config = await get_config(shop_id)
        policy = await get_policy()
        if not config or not policy:
            print("!! shop has no voice config/policy"); return
        resolution = await resolve_caller(shop_id=shop_id, caller_phone=caller)
        call_id = await insert_call(shop_id=shop_id, caller_phone=caller,
                                    matched_customer_id=(resolution.unique_match.customer_id
                                                         if resolution.unique_match else None))
        payload = await build_accept_payload(
            config=config, policy=policy, resolution=resolution,
            model=settings.openai_realtime_model,
        )
        runner = ToolRunner(shop_id=shop_id, call_id=call_id,
                            secret=settings.openai_tool_secret, live=live)

        url = f"wss://api.openai.com/v1/realtime?model={settings.openai_realtime_model}"
        headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
        print(f"(connecting… caller resolved: "
              f"{resolution.unique_match.first_name if resolution.unique_match else 'unknown'})")
        async with websockets.connect(url, additional_headers=headers,
                                      max_size=None) as ws:
            await ws.recv()  # session.created
            await ws.send(json.dumps({"type": "session.update", "session": {
                "type": "realtime",
                "instructions": payload["instructions"],
                "tools": payload["tools"],
                "output_modalities": ["text"],
            }}))
            if messages:
                for msg in messages:
                    await _send_and_run(ws, runner, msg)
            else:
                async for msg in _stdin_messages():
                    await _send_and_run(ws, runner, msg)
    finally:
        await connection.close_connection()


async def _send_and_run(ws, runner: ToolRunner, msg: str) -> None:
    print(f"\nCALLER: {msg}")
    await ws.send(json.dumps({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": msg}],
    }}))
    await _run_response(ws, runner)


async def _stdin_messages():
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line or line.strip() in ("exit", "quit"):
            return
        yield line.strip()


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--live"]
    live = "--live" in sys.argv
    shop_id = UUID(args[0])
    caller = args[1] if len(args) > 1 else ""
    messages = args[2:]
    asyncio.run(chat(shop_id, caller, messages, live))


if __name__ == "__main__":
    main()
