"""Local voice + MCP test harness — mint an OpenAI ephemeral Realtime session
for a browser WebRTC test call against the QA Fly app's MCP server.

The server itself never leaves your machine and executes no tools directly —
OpenAI calls the QA Fly app's MCP server server-to-server for every tool
invocation, exactly like a real Twilio call would. Write tools (create_booking,
etc.) execute for real, but only against the QA Neon branch, not production.

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

from booking_engine.clients.openai_realtime import create_ephemeral_session
from booking_engine.config import Settings
from booking_engine.db.connection import close_connection, init_connection
from booking_engine.db.voice_calls_queries import insert_call
from booking_engine.db.voice_config_queries import get_config, get_policy
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.identity_resolver import resolve_caller
from booking_engine.services.realtime_session import build_accept_payload

logger = logging.getLogger(__name__)

_QA_MCP_URL = "https://kairo-booking-engine-qa.fly.dev/mcp"
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
    mcp_token = mint_call_token(shop_id=shop_id, call_id=call_id,
                                secret=settings.openai_tool_secret)
    payload = await build_accept_payload(
        config=config, policy=policy, resolution=resolution,
        model=settings.openai_realtime_model,
        mcp_server_url=_QA_MCP_URL, mcp_token=mcp_token,
    )
    session = await create_ephemeral_session(
        session_config=payload, api_key=settings.openai_api_key,
    )
    return JSONResponse({"client_secret": session["value"], "call_id": str(call_id)})
