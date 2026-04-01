"""Voice Gateway FastAPI application."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize booking client and OpenAI key."""
    from voice_gateway.config import Settings
    from voice_gateway.clients.booking_client import BookingClient

    settings = Settings()
    logger.info("Voice Gateway starting: booking_url=%s", settings.booking_engine_url)

    bc = BookingClient(base_url=settings.booking_engine_url)
    await bc.__aenter__()
    app.state.booking_client = bc
    app.state._openai_key = settings.openai_key
    logger.info("Booking client connected, OpenAI Realtime %s",
                "enabled" if settings.openai_key else "disabled")

    yield

    if hasattr(app.state, "booking_client") and app.state.booking_client:
        await app.state.booking_client.__aexit__(None, None, None)


def create_app() -> FastAPI:
    import pathlib

    app = FastAPI(title="Virtual Assistant Voice Gateway", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    from voice_gateway.api.routes import realtime
    app.include_router(realtime.router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # Serve test UI
    static_dir = pathlib.Path(__file__).resolve().parent.parent / "static"
    if static_dir.exists():
        from fastapi.responses import FileResponse

        @app.get("/")
        async def ui():
            return FileResponse(static_dir / "index.html")

    return app
