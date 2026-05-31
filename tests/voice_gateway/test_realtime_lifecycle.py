"""Smoke tests for /transcript and /end endpoints (DB / classifier mocked)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voice_gateway.api.routes import realtime
from voice_gateway.call_lifecycle import CallSession


def _app_with_session():
    app = FastAPI()
    app.include_router(realtime.router)
    app.state.call_sessions = {}
    app.state._openai_key = "sk-test"
    app.state._classifier_model = "gpt-4o-mini"
    sess = CallSession(shop_id=uuid4(), caller_number="+39",
                       twilio_call_sid=None)
    sess.id = uuid4()
    sess.started_at = datetime.now(timezone.utc)
    app.state.call_sessions[str(sess.id)] = sess
    return app, sess


def test_transcript_unknown_call_returns_ok_false():
    app, sess = _app_with_session()
    client = TestClient(app)
    r = client.post("/api/v1/realtime/transcript",
                    json={"call_id": "nope", "role": "caller", "text": "hi"})
    assert r.status_code == 200
    assert r.json() == {"ok": False}


def test_transcript_writes_turn():
    app, sess = _app_with_session()
    with patch.object(sess, "append_turn", AsyncMock()) as ap:
        client = TestClient(app)
        r = client.post("/api/v1/realtime/transcript",
                        json={"call_id": str(sess.id), "role": "caller", "text": "hi"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        ap.assert_awaited_once()


def test_end_unknown_call_returns_ok_false():
    app, _ = _app_with_session()
    client = TestClient(app)
    r = client.post("/api/v1/realtime/end", json={"call_id": "nope"})
    assert r.status_code == 200
    assert r.json() == {"ok": False}


def test_end_finalizes_session():
    app, sess = _app_with_session()
    with patch.object(sess, "finalize", AsyncMock()) as fin:
        client = TestClient(app)
        r = client.post("/api/v1/realtime/end", json={"call_id": str(sess.id)})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        fin.assert_awaited_once()
    # Session should be popped
    assert str(sess.id) not in app.state.call_sessions
