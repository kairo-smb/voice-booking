import pytest
from uuid import uuid4

from datetime import datetime, timezone

from booking_engine.config import Settings
from booking_engine.db import token_basket_queries as tbq
from booking_engine.services.messaging import sms_send

SHOP = uuid4()

SETTINGS = Settings(
    webapp_base_url="http://webapp.test", market_intel_secret="test-secret",
)


@pytest.mark.asyncio
async def test_debit_refused_when_the_webapp_refuses(monkeypatch):
    # The webapp's charge-actual returns 402 on an empty basket; that refusal
    # is authoritative (the webapp's deduction is a single locked transaction).
    async def fake_charge(**kw):
        return False
    monkeypatch.setattr(tbq.webapp_credits, "charge_actual", fake_charge)

    ok = await tbq.try_debit_for_message(
        shop_id=SHOP, credits=186, settings=SETTINGS,
    )

    assert ok is False


@pytest.mark.asyncio
async def test_debit_succeeds_and_posts_the_message_as_run_ref(monkeypatch):
    calls = []
    async def fake_charge(**kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(tbq.webapp_credits, "charge_actual", fake_charge)

    msg_id = uuid4()
    ok = await tbq.try_debit_for_message(
        shop_id=SHOP, credits=186, sms_message_id=msg_id, settings=SETTINGS,
    )

    assert ok is True
    assert calls[0]["credits"] == 186
    assert calls[0]["run_type"] == "sms_send"
    assert calls[0]["run_ref"] == str(msg_id)
    assert calls[0]["shop_id"] == SHOP


@pytest.mark.asyncio
async def test_zero_credits_is_a_no_op_success(monkeypatch):
    calls = []
    async def fake_charge(**kw):
        calls.append(kw)
        return True
    monkeypatch.setattr(tbq.webapp_credits, "charge_actual", fake_charge)

    assert await tbq.try_debit_for_message(
        shop_id=SHOP, credits=0, settings=SETTINGS,
    ) is True
    assert calls == []          # a free message writes no ledger row


CUSTOMER = uuid4()
NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def _consenting_customer(**over):
    base = {
        "id": CUSTOMER,
        "full_name": "Giulia Rossi",
        "phone": "+393331234567",
        "phone_normalized": "+393331234567",
        "marketing_consent": True,
        "marketing_consent_granted_at": NOW,
        "marketing_consent_withdrawn_at": None,
    }
    base.update(over)
    return base


class _Recorder:
    """Collects what the service tried to do, so tests assert on effects."""
    def __init__(self):
        self.inserted = None
        self.sent = None
        self.failed = None
        self.debited = None


def _wire(monkeypatch, rec, *, customer, sender="+37251234567",
          debit_ok=True, balance=10_000, twilio=None):
    async def q_balance(shop_id): return balance
    async def q_sender(shop_id): return sender
    async def q_customer(shop_id, customer_id): return customer
    async def q_insert(**kw):
        rec.inserted = kw
        return uuid4()
    async def q_sent(**kw): rec.sent = kw
    async def q_failed(**kw): rec.failed = kw
    async def q_debit(**kw):
        rec.debited = kw
        return debit_ok

    monkeypatch.setattr(sms_send.sms_queries, "get_shop_sender_number", q_sender)
    monkeypatch.setattr(sms_send.sms_queries, "get_customer_for_send", q_customer)
    monkeypatch.setattr(sms_send.sms_queries, "insert_outbound", q_insert)
    monkeypatch.setattr(sms_send.sms_queries, "mark_sent", q_sent)
    monkeypatch.setattr(sms_send.sms_queries, "mark_failed", q_failed)
    monkeypatch.setattr(sms_send.tbq, "get_balance", q_balance)
    monkeypatch.setattr(sms_send.tbq, "try_debit_for_message", q_debit)
    monkeypatch.setattr(
        sms_send, "_twilio_send",
        twilio or (lambda **kw: sms_send.TwilioResult(sid="SM123", price_usd=0.093)),
    )


@pytest.mark.asyncio
async def test_body_sent_is_the_sanitised_input_with_no_suffix(monkeypatch):
    # No STOP footer: opt-out is handled in-store now, not via an in-message
    # reply — the body sent is exactly the sanitised text, nothing appended.
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer())

    await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, settings=SETTINGS, body="Ciao Giulia, ti aspettiamo!"
    )

    assert rec.inserted["body"] == "Ciao Giulia, ti aspettiamo!"


@pytest.mark.asyncio
async def test_withdrawn_consent_is_suppressed_not_sent(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec,
          customer=_consenting_customer(marketing_consent_withdrawn_at=NOW))

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, settings=SETTINGS, body="Ciao"
    )

    assert result.ok is False
    assert result.reason == "no_consent"
    assert rec.inserted["status"] == "suppressed"
    assert rec.sent is None


@pytest.mark.asyncio
async def test_insufficient_credits_blocks_the_send(monkeypatch):
    # Never send something that can't be billed.
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer(), balance=0)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, settings=SETTINGS, body="Ciao"
    )

    assert result.ok is False
    assert result.reason == "insufficient_credits"
    assert rec.sent is None
    assert rec.debited is None      # refused before any money moved


@pytest.mark.asyncio
async def test_provider_failure_does_not_charge_the_shop(monkeypatch):
    # Twilio rejected it, so Twilio charged us nothing. Debiting before the
    # provider call would have billed the salon for a message that never went.
    rec = _Recorder()
    def boom(**kw):
        raise RuntimeError("21610 unsubscribed recipient")
    _wire(monkeypatch, rec, customer=_consenting_customer(), twilio=boom)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, settings=SETTINGS, body="Ciao"
    )

    assert result.reason == "provider_error"
    assert rec.debited is None


@pytest.mark.asyncio
async def test_curly_quote_from_the_llm_does_not_double_the_bill(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer())

    await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, settings=SETTINGS, body="Ciao Giulia, com’è andata?"
    )

    assert rec.inserted["encoding"] == "gsm7"
    assert "’" not in rec.inserted["body"]


@pytest.mark.asyncio
async def test_successful_send_charges_two_times_twilio(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer())

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, settings=SETTINGS, body="Ciao Giulia!"
    )

    assert result.ok is True
    assert rec.debited["credits"] == 186      # 0.093 × 2 × 1000
    assert rec.sent["provider_sid"] == "SM123"


@pytest.mark.asyncio
async def test_twilio_failure_marks_the_row_failed(monkeypatch):
    rec = _Recorder()
    def boom(**kw):
        raise RuntimeError("21610 unsubscribed recipient")
    _wire(monkeypatch, rec, customer=_consenting_customer(), twilio=boom)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, settings=SETTINGS, body="Ciao"
    )

    assert result.ok is False
    assert result.reason == "provider_error"
    assert rec.failed is not None


@pytest.mark.asyncio
async def test_shop_without_a_number_cannot_send(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer(), sender=None)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, settings=SETTINGS, body="Ciao"
    )

    assert result.reason == "no_sender_number"


@pytest.mark.asyncio
async def test_status_callback_url_is_passed_to_the_provider(monkeypatch):
    # Without this Twilio never calls POST /sms/webhook/status, so the row
    # stays 'sent' forever and price_usd stays NULL forever (CLAUDE.md).
    rec = _Recorder()
    captured = {}

    def fake_twilio(**kw):
        captured.update(kw)
        return sms_send.TwilioResult(sid="SM123", price_usd=0.093)

    _wire(monkeypatch, rec, customer=_consenting_customer(), twilio=fake_twilio)

    await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, settings=SETTINGS, body="Ciao Giulia!",
        public_base_url="https://api.example.com",
    )

    assert captured["status_callback"] == "https://api.example.com/api/v1/sms/webhook/status"


@pytest.mark.asyncio
async def test_status_callback_is_none_when_public_base_url_is_unset(monkeypatch):
    rec = _Recorder()
    captured = {}

    def fake_twilio(**kw):
        captured.update(kw)
        return sms_send.TwilioResult(sid="SM123", price_usd=0.093)

    _wire(monkeypatch, rec, customer=_consenting_customer(), twilio=fake_twilio)

    await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, settings=SETTINGS, body="Ciao Giulia!",
    )

    assert captured["status_callback"] is None
