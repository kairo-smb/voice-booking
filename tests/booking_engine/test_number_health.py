from uuid import uuid4

import pytest

from booking_engine import config as config_module
from booking_engine.services import number_health
from booking_engine.services.number_health import HealthProbe, decide_health

BASE = "https://api.example.com"
VOICE = f"{BASE}/api/v1/voice/twiml/incoming"
SMS = f"{BASE}/api/v1/sms/webhook/inbound"


def test_healthy_number_is_green():
    probe = HealthProbe(found=True, voice_url=VOICE, sms_url=SMS, reachable=True)
    assert decide_health(probe, base_url=BASE) == ("green", None)


def test_missing_number_is_red():
    # Twilio 404: released, deleted, or moved to another account.
    probe = HealthProbe(found=False, voice_url="", sms_url="", reachable=True)
    assert decide_health(probe, base_url=BASE) == ("red", "number_missing")


def test_webhook_drift_is_red():
    probe = HealthProbe(found=True, voice_url="https://old.example.com/hook",
                        sms_url=SMS, reachable=True)
    assert decide_health(probe, base_url=BASE) == ("red", "webhook_drift")


def test_twilio_unreachable_leaves_the_previous_verdict():
    """A provider outage must not repaint every salon red. None = 'no verdict',
    which set_health turns into 'stamp health_checked_at, leave health_status'."""
    probe = HealthProbe(found=False, voice_url="", sms_url="", reachable=False)
    assert decide_health(probe, base_url=BASE) == (None, "provider_unreachable")


def test_unreachable_wins_over_missing():
    # An unreachable probe cannot prove absence — order matters.
    probe = HealthProbe(found=False, voice_url="", sms_url="", reachable=False)
    status, _ = decide_health(probe, base_url=BASE)
    assert status is None


def test_empty_sms_url_is_drift_not_green():
    # A number with no SMS webhook cannot receive STOP — that is a real fault.
    probe = HealthProbe(found=True, voice_url=VOICE, sms_url="", reachable=True)
    assert decide_health(probe, base_url=BASE) == ("red", "webhook_drift")


@pytest.mark.asyncio
async def test_check_all_mixed_outcomes(monkeypatch):
    from twilio.base.exceptions import TwilioRestException

    from booking_engine.clients.twilio_numbers import NumberStatus

    shop_missing = uuid4()
    shop_healthy = uuid4()
    shop_broken = uuid4()

    rows = [
        {"shop_id": shop_missing, "kairo_number": "+37200000001", "kairo_number_sid": "SIDMISSING"},
        {"shop_id": shop_healthy, "kairo_number": "+37200000002", "kairo_number_sid": "SIDHEALTHY"},
        {"shop_id": shop_broken, "kairo_number": "+37200000003", "kairo_number_sid": "SIDBROKEN"},
    ]

    async def fake_list_provisioned_numbers():
        return rows

    def fake_fetch_number(*, sid, account_sid, auth_token):
        if sid == "SIDMISSING":
            raise TwilioRestException(status=404, uri="/x", msg="not found")
        if sid == "SIDHEALTHY":
            return NumberStatus(sid=sid, phone_number="+37200000002", voice_url=VOICE, sms_url=SMS)
        if sid == "SIDBROKEN":
            raise RuntimeError("boom")
        raise AssertionError("unexpected sid")

    recorded = []

    async def fake_set_health(*, shop_id, status, detail):
        recorded.append((shop_id, status, detail))

    monkeypatch.setattr(number_health, "list_provisioned_numbers", fake_list_provisioned_numbers)
    monkeypatch.setattr(number_health, "fetch_number", fake_fetch_number)
    monkeypatch.setattr(number_health, "set_health", fake_set_health)

    settings = config_module.Settings(
        twilio_account_sid="AC123",
        twilio_auth_token="tok",
        public_base_url=BASE,
    )

    counts = await number_health.check_all(settings=settings)

    by_shop = {shop_id: (status, detail) for shop_id, status, detail in recorded}
    assert by_shop[shop_missing] == ("red", "number_missing")
    assert by_shop[shop_healthy] == ("green", None)
    assert by_shop[shop_broken][0] is None

    assert counts == {"checked": 3, "green": 1, "red": 1, "inconclusive": 1}
