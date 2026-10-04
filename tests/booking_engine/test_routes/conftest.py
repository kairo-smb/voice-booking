"""Fixtures for booking engine route tests — TestClient with mocked DB."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from booking_engine.api.routes import shops, customers, services, appointments


@pytest.fixture
def app() -> FastAPI:
    """Create a bare FastAPI app with all routers, no lifespan (no real DB)."""
    app = FastAPI()
    app.include_router(shops.router, prefix="/api/v1")
    app.include_router(customers.router, prefix="/api/v1")
    app.include_router(services.router, prefix="/api/v1")
    app.include_router(appointments.router, prefix="/api/v1")
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)
