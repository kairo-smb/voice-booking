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


def test_list_calls_filters_passed_through():
    fake = {"items": [], "next_cursor": None}
    with patch.object(voice.vq, "list_calls",
                      AsyncMock(return_value=fake)) as lc:
        client = TestClient(_app())
        shop = uuid4()
        r = client.get(
            f"/api/v1/shops/{shop}/voice/calls"
            f"?outcome=booked&outcome=abandoned&q=mario&limit=5",
            headers=HEADERS,
        )
        assert r.status_code == 200
        body = r.json()["data"]
        assert body == []
        kwargs = lc.await_args.kwargs
        assert kwargs["filters"]["outcome"] == ["booked", "abandoned"]
        assert kwargs["filters"]["q"] == "mario"
        assert kwargs["limit"] == 5


def test_list_calls_returns_items_and_cursor():
    item = {
        "id": uuid4(), "caller_number": "+39", "customer_id": None,
        "customer_match": "unmatched",
        "started_at": datetime.now(timezone.utc),
        "ended_at": None, "duration_seconds": None,
        "outcome": None, "summary": None, "appointment_id": None,
    }
    with patch.object(voice.vq, "list_calls",
                      AsyncMock(return_value={"items": [item], "next_cursor": "abc"})):
        client = TestClient(_app())
        r = client.get(f"/api/v1/shops/{uuid4()}/voice/calls", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert len(body["data"]) == 1
        assert body["next_cursor"] == "abc"


def test_get_call_detail_404():
    with patch.object(voice.vq, "get_call_detail", AsyncMock(return_value=None)):
        client = TestClient(_app())
        r = client.get(
            f"/api/v1/shops/{uuid4()}/voice/calls/{uuid4()}",
            headers=HEADERS,
        )
        assert r.status_code == 404


def test_get_call_detail_ok():
    call = {
        "id": uuid4(), "caller_number": "+39", "customer_id": None,
        "customer_match": "existing",
        "started_at": datetime.now(timezone.utc),
        "ended_at": None, "duration_seconds": None,
        "outcome": "booked", "summary": "ok", "appointment_id": None,
    }
    turn = {"turn_index": 0, "role": "assistant", "text": "Ciao",
            "at": datetime.now(timezone.utc)}
    ev = {"at": datetime.now(timezone.utc), "type": "function_call", "payload": {}}
    with patch.object(voice.vq, "get_call_detail", AsyncMock(return_value={
        "call": call, "transcript": [turn], "events": [ev],
    })):
        client = TestClient(_app())
        r = client.get(
            f"/api/v1/shops/{uuid4()}/voice/calls/{uuid4()}",
            headers=HEADERS,
        )
        assert r.status_code == 200
        assert len(r.json()["data"]["transcript"]) == 1


def test_link_customer_validates_body():
    client = TestClient(_app())
    r = client.patch(
        f"/api/v1/shops/{uuid4()}/voice/calls/{uuid4()}/link-customer",
        headers=HEADERS, json={"customer_id": "not-a-uuid"},
    )
    assert r.status_code == 422


def test_link_customer_ok():
    updated = {
        "id": uuid4(), "caller_number": "+39",
        "customer_id": uuid4(), "customer_match": "existing",
        "started_at": datetime.now(timezone.utc),
        "ended_at": None, "duration_seconds": None,
        "outcome": None, "summary": None, "appointment_id": None,
    }
    with patch.object(voice.vq, "link_customer",
                      AsyncMock(return_value=updated)):
        client = TestClient(_app())
        r = client.patch(
            f"/api/v1/shops/{uuid4()}/voice/calls/{uuid4()}/link-customer",
            headers=HEADERS,
            json={"customer_id": str(uuid4())},
        )
        assert r.status_code == 200
        assert r.json()["data"]["customer_match"] == "existing"


def test_link_customer_404():
    with patch.object(voice.vq, "link_customer", AsyncMock(return_value=None)):
        client = TestClient(_app())
        r = client.patch(
            f"/api/v1/shops/{uuid4()}/voice/calls/{uuid4()}/link-customer",
            headers=HEADERS, json={"customer_id": str(uuid4())},
        )
        assert r.status_code == 404
