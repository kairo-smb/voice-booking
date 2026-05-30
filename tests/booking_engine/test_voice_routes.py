"""Route handler tests for /voice/* endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from booking_engine.api.routes import voice
from booking_engine.config import Settings


def _app(secret: str = "test-secret") -> FastAPI:
    app = FastAPI()
    app.include_router(voice.router, prefix="/api/v1")
    from booking_engine.api import deps
    app.dependency_overrides[deps._get_settings] = lambda: Settings(
        database_url="", control_plane_secret=secret,
    )
    return app


HEADERS = {"Authorization": "Bearer test-secret"}


def test_get_config_unauthorized():
    client = TestClient(_app())
    r = client.get(f"/api/v1/shops/{uuid4()}/voice/config")
    assert r.status_code == 401


def test_get_config_not_found():
    with patch.object(voice.vq, "get_voice_config", AsyncMock(return_value=None)):
        client = TestClient(_app())
        r = client.get(f"/api/v1/shops/{uuid4()}/voice/config", headers=HEADERS)
        assert r.status_code == 404


def test_get_config_ok():
    fake = {
        "welcome_message": "Ciao",
        "tone_instructions": None, "personality": None, "special_instructions": None,
        "voice": "alloy", "language": "it", "is_active": True,
    }
    with patch.object(voice.vq, "get_voice_config", AsyncMock(return_value=fake)):
        client = TestClient(_app())
        r = client.get(f"/api/v1/shops/{uuid4()}/voice/config", headers=HEADERS)
        assert r.status_code == 200
        assert r.json()["data"]["voice"] == "alloy"


def test_patch_config_validates_body():
    client = TestClient(_app())
    r = client.patch(
        f"/api/v1/shops/{uuid4()}/voice/config",
        headers=HEADERS, json={"language": "fr"},
    )
    assert r.status_code == 422


def test_patch_config_updates_and_returns_config():
    fake = {
        "welcome_message": "Aggiornato",
        "tone_instructions": None, "personality": None, "special_instructions": None,
        "voice": "echo", "language": "it", "is_active": True,
    }
    with patch.object(voice.vq, "update_voice_config",
                      AsyncMock(return_value=fake)) as upd:
        client = TestClient(_app())
        r = client.patch(
            f"/api/v1/shops/{uuid4()}/voice/config",
            headers=HEADERS, json={"welcome_message": "Aggiornato", "voice": "echo"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["voice"] == "echo"
        upd.assert_awaited_once()
