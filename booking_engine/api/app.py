"""Booking Engine FastAPI application."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from booking_engine.config import Settings
from booking_engine.db.connection import init_connection, close_connection

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    logger.info("Connecting to PostgreSQL...")
    await init_connection(settings)
    logger.info("PostgreSQL connection pool ready")
    yield
    await close_connection()


def create_app() -> FastAPI:
    app = FastAPI(title="Virtual Assistant Booking Engine", version="1.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    from booking_engine.api.routes import shops, customers, services, availability, appointments
    app.include_router(shops.router, prefix="/api/v1")
    app.include_router(customers.router, prefix="/api/v1")
    app.include_router(services.router, prefix="/api/v1")
    app.include_router(availability.router, prefix="/api/v1")
    app.include_router(appointments.router, prefix="/api/v1")
    from booking_engine.api.routes import voice  # noqa: WPS433
    app.include_router(voice.router, prefix="/api/v1")
    from booking_engine.api.routes import voice_telephony
    app.include_router(voice_telephony.router, prefix="/api/v1")
    from booking_engine.api.routes import voice_twiml
    app.include_router(voice_twiml.router, prefix="/api/v1")
    from booking_engine.api.routes import voice_openai
    app.include_router(voice_openai.router)
    from booking_engine.api.routes import voice_config
    app.include_router(voice_config.router, prefix="/api/v1")
    from booking_engine.api.routes import voice_balance
    app.include_router(voice_balance.router, prefix="/api/v1")
    from booking_engine.api.routes import voice_heartbeat
    app.include_router(voice_heartbeat.router, prefix="/api/v1")
    from booking_engine.api.routes import voice_tools_catalog
    app.include_router(voice_tools_catalog.router)
    from booking_engine.api.routes import voice_tools_booking
    app.include_router(voice_tools_booking.router)
    from booking_engine.api.routes import voice_tools_lifecycle
    app.include_router(voice_tools_lifecycle.router)
    from booking_engine.api.routes import voice_events
    app.include_router(voice_events.router)
    from booking_engine.api.routes import voice_memos
    app.include_router(voice_memos.router)
    from booking_engine.api.routes import voice_tools_identity
    app.include_router(voice_tools_identity.router)

    # Remote MCP server for OpenAI Realtime tool calls (session manager is run
    # by the production entrypoint's lifespan, see booking_engine/asgi.py).
    from booking_engine.mcp_server import mcp_asgi, set_app
    app.mount("/mcp", mcp_asgi)
    set_app(app)

    @app.get("/health")
    async def health():
        return {"status": "ok"}
    return app
