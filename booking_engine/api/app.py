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

    @app.get("/health")
    async def health():
        return {"status": "ok"}
    return app
