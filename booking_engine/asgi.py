"""Production ASGI entrypoint (uvicorn booking_engine.asgi:app).

Wraps create_app() with a lifespan that initializes the DB pool and starts the
scheduler. (It also ran the MCP session manager until 2026-09-28, when the voice
tools moved to marketing-engine's customer agents.)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from booking_engine import observability

from booking_engine.api.app import create_app
from booking_engine.config import Settings
from booking_engine.db.connection import close_connection, init_connection
from booking_engine.services import scheduler


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = Settings()
    await init_connection(settings)
    jobs = scheduler.start(settings)
    yield
    for job in jobs:
        job.cancel()
    await close_connection()


observability.init()  # before create_app, so the FastAPI integration hooks it
app = create_app()
app.router.lifespan_context = _lifespan
