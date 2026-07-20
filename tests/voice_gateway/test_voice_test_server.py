from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import scripts.voice_test_server as server
from booking_engine.services.identity_resolver import ResolutionResult


def _config():
    return {"welcome_message": "hi", "tone_instructions": "", "personality": "",
            "special_instructions": ""}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DEMO_SHOP_ID", str(uuid4()))
    return TestClient(server.app)


def test_session_returns_client_secret_and_targets_qa_mcp(client):
    shop_id = uuid4()
    build_payload_mock = AsyncMock(
        return_value={"type": "realtime", "model": "gpt-realtime"}
    )
    create_session_mock = AsyncMock(
        return_value={"value": "ek_test123", "expires_at": 1}
    )

    with patch("scripts.voice_test_server.get_config",
               new=AsyncMock(return_value=_config())), \
         patch("scripts.voice_test_server.get_policy",
               new=AsyncMock(return_value={"disclosure_text": "hi"})), \
         patch("scripts.voice_test_server.resolve_caller",
               new=AsyncMock(return_value=ResolutionResult(is_anonymous=True, matches=[]))), \
         patch("scripts.voice_test_server.insert_call",
               new=AsyncMock(return_value=uuid4())), \
         patch("scripts.voice_test_server.build_accept_payload", new=build_payload_mock), \
         patch("scripts.voice_test_server.create_ephemeral_session", new=create_session_mock):
        resp = client.post("/session", json={"shop_id": str(shop_id), "caller_phone": "+391234"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["client_secret"] == "ek_test123"
    assert "call_id" in body

    _, kwargs = build_payload_mock.call_args
    assert kwargs["mcp_server_url"] == "https://kairo-booking-engine-qa.fly.dev/mcp"
    assert kwargs["mcp_token"]

    create_session_mock.assert_awaited_once()
    _, create_kwargs = create_session_mock.call_args
    assert create_kwargs["session_config"] == build_payload_mock.return_value


def test_session_defaults_shop_id_from_env(client, monkeypatch):
    demo_shop_id = str(uuid4())
    monkeypatch.setenv("DEMO_SHOP_ID", demo_shop_id)
    build_payload_mock = AsyncMock(return_value={"type": "realtime"})
    resolve_mock = AsyncMock(return_value=ResolutionResult(is_anonymous=True, matches=[]))

    with patch("scripts.voice_test_server.get_config", new=AsyncMock(return_value=_config())), \
         patch("scripts.voice_test_server.get_policy",
               new=AsyncMock(return_value={"disclosure_text": "hi"})), \
         patch("scripts.voice_test_server.resolve_caller", new=resolve_mock), \
         patch("scripts.voice_test_server.insert_call", new=AsyncMock(return_value=uuid4())), \
         patch("scripts.voice_test_server.build_accept_payload", new=build_payload_mock), \
         patch("scripts.voice_test_server.create_ephemeral_session",
               new=AsyncMock(return_value={"value": "ek_x", "expires_at": 1})):
        resp = client.post("/session", json={})

    assert resp.status_code == 200
    resolve_mock.assert_awaited_once()
    _, kwargs = resolve_mock.call_args
    from uuid import UUID
    assert kwargs["shop_id"] == UUID(demo_shop_id)


def test_session_returns_400_when_shop_has_no_config(client):
    with patch("scripts.voice_test_server.get_config", new=AsyncMock(return_value=None)), \
         patch("scripts.voice_test_server.get_policy",
               new=AsyncMock(return_value={"disclosure_text": "hi"})):
        resp = client.post("/session", json={"shop_id": str(uuid4())})

    assert resp.status_code == 400
