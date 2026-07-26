"""Production ASGI entrypoint (uvicorn booking_engine.asgi:app).

Wraps create_app() with a lifespan that initializes the DB pool AND runs the
MCP session manager. Tests use create_app() directly and don't need the MCP
session manager running, so it's kept out of the shared app lifespan.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from booking_engine.api.app import create_app
from booking_engine.config import Settings
from booking_engine.db.connection import close_connection, init_connection
from booking_engine.mcp_server import mcp_lifespan


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await init_connection(Settings())
    async with mcp_lifespan():
        yield
    await close_connection()


app = create_app()
app.router.lifespan_context = _lifespan
