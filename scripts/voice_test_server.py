"""Local voice test harness — mint an OpenAI ephemeral Realtime session for a
browser WebRTC test call, with the same tools and rules a real call gets.

The server itself never leaves your machine and executes no tools — OpenAI
calls marketing-engine's customer-agents MCP (`{MARKET_INTEL_API_URL}/
customer-agents/voice/mcp`) server-to-server for every tool invocation, exactly
like a real Twilio call would. Point `DATABASE_URL` and `MARKET_INTEL_API_URL`
at the same environment (QA): the engine loads the call row this harness
inserts. Write tools (create_appointment, ...) execute for real there.

Usage:
    export PYTHONPATH=. ; set -a; source .env; set +a
    uvicorn scripts.voice_test_server:app --port 8765
    # then open http://localhost:8765/ in a browser
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from booking_engine.clients.customer_agents_voice import fetch_instructions, mcp_url
from booking_engine.clients.openai_realtime import create_ephemeral_session
from booking_engine.config import Settings
from booking_engine.db.connection import close_connection, init_connection
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.db.voice_config_queries import get_config, get_policy
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.identity_resolver import resolve_caller
from booking_engine.services.realtime_session import build_accept_payload

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "voice_test_static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_connection(Settings())
    yield
    await close_connection()


app = FastAPI(lifespan=lifespan)


class SessionRequest(BaseModel):
    shop_id: UUID | None = None
    caller_phone: str = ""


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.post("/session")
async def create_session(body: SessionRequest) -> JSONResponse:
    settings = Settings()
    shop_id = body.shop_id or UUID(os.environ["DEMO_SHOP_ID"])

    config = await get_config(shop_id)
    policy = await get_policy()
    if not config or not policy:
        return JSONResponse({"error": "shop has no voice config/policy"}, status_code=400)

    resolution = await resolve_caller(shop_id=shop_id, caller_phone=body.caller_phone)
    call_id = await insert_call(
        shop_id=shop_id, caller_phone=body.caller_phone,
        matched_customer_id=(resolution.unique_match.customer_id
                              if resolution.unique_match else None),
    )
    server_url = mcp_url(settings)
    if not server_url or not settings.voice_agent_tool_secret:
        return JSONResponse(
            {"error": "set MARKET_INTEL_API_URL and VOICE_AGENT_TOOL_SECRET"},
            status_code=400)
    mcp_token = mint_call_token(shop_id=shop_id, call_id=call_id,
                                secret=settings.voice_agent_tool_secret)
    instructions = await fetch_instructions(
        shop_id=shop_id, call_id=call_id, token=mcp_token, settings=settings)
    payload = await build_accept_payload(
        config=config, policy=policy, resolution=resolution,
        model=settings.openai_realtime_model,
        mcp_server_url=server_url, mcp_token=mcp_token,
        agent_instructions=instructions,
    )
    session = await create_ephemeral_session(
        session_config=payload, api_key=settings.openai_api_key,
    )
    return JSONResponse({"client_secret": session["value"], "call_id": str(call_id)})
