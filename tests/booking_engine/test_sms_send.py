import pytest
from uuid import uuid4

from datetime import datetime, timezone

from booking_engine.db import token_basket_queries as tbq
from booking_engine.services.messaging import sms_send

SHOP = uuid4()


@pytest.mark.asyncio
async def test_debit_refused_when_balance_is_short(monkeypatch):
    calls = []
    async def fake_balance(shop_id):
        return 100
    async def fake_insert(**kw):
        calls.append(kw)
    monkeypatch.setattr(tbq, "get_balance", fake_balance)
    monkeypatch.setattr(tbq, "insert_debit_event", fake_insert)

    ok = await tbq.try_debit_for_message(shop_id=SHOP, credits=186)

    assert ok is False
    assert calls == []          # nothing was debited


@pytest.mark.asyncio
async def test_debit_succeeds_and_records_the_message(monkeypatch):
    calls = []
    async def fake_balance(shop_id):
        return 1000
    async def fake_insert(**kw):
        calls.append(kw)
    monkeypatch.setattr(tbq, "get_balance", fake_balance)
    monkeypatch.setattr(tbq, "insert_debit_event", fake_insert)

    msg_id = uuid4()
    ok = await tbq.try_debit_for_message(shop_id=SHOP, credits=186, sms_message_id=msg_id)

    assert ok is True
    assert calls[0]["tokens"] == 186
    assert calls[0]["sms_message_id"] == msg_id


@pytest.mark.asyncio
async def test_zero_credits_is_a_no_op_success(monkeypatch):
    calls = []
    async def fake_insert(**kw):
        calls.append(kw)
    monkeypatch.setattr(tbq, "insert_debit_event", fake_insert)

    assert await tbq.try_debit_for_message(shop_id=SHOP, credits=0) is True
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


def _wire(monkeypatch, rec, *, customer, opted_out=False, sender="+37251234567",
          debit_ok=True, twilio=None):
    async def q_sender(shop_id): return sender
    async def q_customer(shop_id, customer_id): return customer
    async def q_opted(shop_id, phone): return opted_out
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
    monkeypatch.setattr(sms_send.sms_queries, "is_opted_out", q_opted)
    monkeypatch.setattr(sms_send.sms_queries, "insert_outbound", q_insert)
    monkeypatch.setattr(sms_send.sms_queries, "mark_sent", q_sent)
    monkeypatch.setattr(sms_send.sms_queries, "mark_failed", q_failed)
    monkeypatch.setattr(sms_send.tbq, "try_debit_for_message", q_debit)
    monkeypatch.setattr(
        sms_send, "_twilio_send",
        twilio or (lambda **kw: sms_send.TwilioResult(sid="SM123", price_usd=0.093)),
    )


@pytest.mark.asyncio
async def test_opt_out_footer_is_appended_server_side(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer())

    await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao Giulia, ti aspettiamo!"
    )

    assert rec.inserted["body"].endswith(sms_send.OPT_OUT_FOOTER)


@pytest.mark.asyncio
async def test_withdrawn_consent_is_suppressed_not_sent(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec,
          customer=_consenting_customer(marketing_consent_withdrawn_at=NOW))

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao"
    )

    assert result.ok is False
    assert result.reason == "no_consent"
    assert rec.inserted["status"] == "suppressed"
    assert rec.sent is None


@pytest.mark.asyncio
async def test_opted_out_phone_is_suppressed(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer(), opted_out=True)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao"
    )

    assert result.reason == "opted_out"
    assert rec.sent is None


@pytest.mark.asyncio
async def test_insufficient_credits_blocks_the_send(monkeypatch):
    # Never send something that can't be billed.
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer(), debit_ok=False)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao"
    )

    assert result.ok is False
    assert result.reason == "insufficient_credits"
    assert rec.sent is None


@pytest.mark.asyncio
async def test_curly_quote_from_the_llm_does_not_double_the_bill(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer())

    await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao Giulia, com’è andata?"
    )

    assert rec.inserted["encoding"] == "gsm7"
    assert "’" not in rec.inserted["body"]


@pytest.mark.asyncio
async def test_successful_send_charges_two_times_twilio(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer())

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao Giulia!"
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
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao"
    )

    assert result.ok is False
    assert result.reason == "provider_error"
    assert rec.failed is not None


@pytest.mark.asyncio
async def test_shop_without_a_number_cannot_send(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, customer=_consenting_customer(), sender=None)

    result = await sms_send.send_marketing_sms(
        shop_id=SHOP, customer_id=CUSTOMER, body="Ciao"
    )

    assert result.reason == "no_sender_number"
