"""Production ASGI entrypoint (uvicorn booking_engine.asgi:app).

Wraps create_app() with a lifespan that initializes the DB pool AND runs the
MCP session manager. Tests use create_app() directly and don't need the MCP
session manager running, so it's kept out of the shared app lifespan.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from booking_engine.api.app import create_app
from booking_engine.config import Settings
from booking_engine.db.connection import close_connection, init_connection
from booking_engine.mcp_server import mcp_lifespan
from booking_engine.services.messaging.whatsapp_send import send_loop


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = Settings()
    await init_connection(settings)
    loop = (asyncio.create_task(send_loop(settings=settings))
            if settings.whatsapp_send_loop_seconds > 0 else None)
    async with mcp_lifespan():
        yield
    if loop:
        loop.cancel()
    await close_connection()


app = create_app()
app.router.lifespan_context = _lifespan
