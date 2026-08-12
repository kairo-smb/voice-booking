import pytest
from uuid import uuid4
from fastapi.testclient import TestClient

from booking_engine.api.app import create_app

SHOP, CUSTOMER = str(uuid4()), str(uuid4())


@pytest.fixture
def client():
    return TestClient(create_app())


def test_send_requires_the_control_plane_token(client):
    r = client.post("/api/v1/sms/send",
                    json={"shop_id": SHOP, "customer_id": CUSTOMER, "body": "Ciao"})
    assert r.status_code in (401, 503)


def test_send_rejects_an_empty_body(client, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", "s3cret")
    r = client.post("/api/v1/sms/send",
                    headers={"Authorization": "Bearer s3cret"},
                    json={"shop_id": SHOP, "customer_id": CUSTOMER, "body": "   "})
    assert r.status_code == 422


def test_suppressed_send_returns_409_with_the_reason(client, monkeypatch):
    from booking_engine.api.routes import sms as sms_routes
    from booking_engine.services.messaging.sms_send import SendResult

    monkeypatch.setenv("CONTROL_PLANE_SECRET", "s3cret")

    async def fake_send(**kw):
        return SendResult(ok=False, reason="no_consent")
    monkeypatch.setattr(sms_routes, "send_marketing_sms", fake_send)

    r = client.post("/api/v1/sms/send",
                    headers={"Authorization": "Bearer s3cret"},
                    json={"shop_id": SHOP, "customer_id": CUSTOMER, "body": "Ciao"})
    assert r.status_code == 409
    assert r.json()["detail"] == "no_consent"
