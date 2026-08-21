from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from booking_engine.db import sms_queries
from booking_engine.db import token_basket_queries as tbq
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import whatsapp_send as ws
from booking_engine.services.messaging import whatsapp_templates as wt

SHOP = uuid4()
ROME = ZoneInfo("Europe/Rome")


class FakeSettings:
    twilio_account_sid = "ACtest"
    twilio_auth_token = "token"
    public_base_url = "https://example.test"
    whatsapp_send_start_hour = 9
    whatsapp_send_end_hour = 20


def _consenting(**over):
    row = {
        "id": uuid4(), "full_name": "Giulia", "phone": "+393331112222",
        "phone_normalized": "393331112222", "marketing_consent": True,
        "marketing_consent_granted_at": datetime(2026, 1, 1, tzinfo=ROME),
        "marketing_consent_withdrawn_at": None,
    }
    row.update(over)
    return row


# ----------------------------------------------------------------- scheduling

def test_spread_fills_the_rest_of_todays_window():
    now = datetime(2026, 8, 20, 10, 0, tzinfo=ROME)
    times = ws.spread(5, now, start_hour=9, end_hour=20)

    assert len(times) == 5
    assert times[0] == now                      # first goes out immediately
    assert times[-1] < datetime(2026, 8, 20, 20, 0, tzinfo=ROME)
    assert times == sorted(times)


def test_spread_before_the_window_starts_at_opening():
    times = ws.spread(3, datetime(2026, 8, 20, 6, 0, tzinfo=ROME),
                      start_hour=9, end_hour=20)
    assert times[0] == datetime(2026, 8, 20, 9, 0, tzinfo=ROME)


def test_spread_after_the_window_rolls_to_tomorrow():
    times = ws.spread(2, datetime(2026, 8, 20, 22, 30, tzinfo=ROME),
                      start_hour=9, end_hour=20)
    assert times[0] == datetime(2026, 8, 21, 9, 0, tzinfo=ROME)


def test_spread_of_fifty_never_bunches_them_together():
    """The whole point of the feature: 50/day distributed, not 50 at once."""
    now = datetime(2026, 8, 20, 9, 0, tzinfo=ROME)
    times = ws.spread(50, now, start_hour=9, end_hour=20)

    gaps = {(b - a).total_seconds() for a, b in zip(times, times[1:])}
    assert len(times) == 50
    assert min(gaps) > 600                       # >10 minutes apart, every pair
    assert times[-1] < datetime(2026, 8, 20, 20, 0, tzinfo=ROME)


def test_spread_of_one_goes_now_not_at_the_window_midpoint():
    now = datetime(2026, 8, 20, 15, 0, tzinfo=ROME)
    assert ws.spread(1, now, start_hour=9, end_hour=20) == [now]


def test_spread_of_zero_is_empty():
    assert ws.spread(0, datetime(2026, 8, 20, 12, 0, tzinfo=ROME),
                     start_hour=9, end_hour=20) == []


# ------------------------------------------------------------------ templates

def test_clean_variable_strips_what_meta_rejects():
    """Newlines, tabs and 4+ spaces in a parameter are a hard Meta rejection."""
    dirty = "  offerta\nspeciale\tdi    agosto  "
    assert wt.clean_variable(dirty) == "offerta speciale di agosto"


def test_clean_variable_bounds_length():
    assert len(wt.clean_variable("x" * 5000)) == wt.MAX_VARIABLE_CHARS


def test_render_produces_what_the_customer_reads():
    text = wt.render("promo_v1", {"1": "Giulia", "2": "Salone X", "3": "sconto 20%."})
    assert "Ciao Giulia!" in text
    assert "Salone X" in text
    assert "{{" not in text


def test_every_catalogue_template_has_a_sample_for_each_variable():
    """Meta rejects a body starting with a variable unless a sample is sent."""
    for key, tpl in wt.CATALOGUE.items():
        for n in range(1, tpl.variables + 1):
            assert str(n) in tpl.sample, f"{key} is missing a sample for {{{{{n}}}}}"


# --------------------------------------------------------------------- gating

@pytest.mark.asyncio
async def test_enqueue_refuses_when_the_sender_is_not_online(monkeypatch):
    async def sender(shop_id):
        return {"status": "verifying"}
    monkeypatch.setattr(wq, "get_sender", sender)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}}],
        settings=FakeSettings(),
    )
    assert result == {"ok": False, "error": "sender_not_online"}


@pytest.mark.asyncio
async def test_enqueue_refuses_an_unapproved_template(monkeypatch):
    async def sender(shop_id):
        return {"status": "online", "phone_number": "+3721234567", "daily_cap": 50}
    async def template(shop_id, key):
        return {"status": "rejected", "content_sid": "HX1"}
    monkeypatch.setattr(wq, "get_sender", sender)
    monkeypatch.setattr(wq, "get_template", template)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}}],
        settings=FakeSettings(),
    )
    assert result["error"] == "template_rejected"


@pytest.mark.asyncio
async def test_enqueue_refuses_more_recipients_than_the_daily_cap(monkeypatch):
    async def sender(shop_id):
        return {"status": "online", "phone_number": "+3721234567", "daily_cap": 50}
    async def template(shop_id, key):
        return {"status": "approved", "content_sid": "HX1"}
    monkeypatch.setattr(wq, "get_sender", sender)
    monkeypatch.setattr(wq, "get_template", template)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}} for _ in range(51)],
        settings=FakeSettings(),
    )
    assert result == {"ok": False, "error": "over_daily_cap", "daily_cap": 50}


@pytest.mark.asyncio
async def test_enqueue_records_a_suppressed_row_for_no_consent(monkeypatch):
    """A refusal is a row, never silence: 'why did Giulia not get it?'"""
    rows = []

    async def sender(shop_id):
        return {"status": "online", "phone_number": "+3721234567", "daily_cap": 50}
    async def template(shop_id, key):
        return {"status": "approved", "content_sid": "HX1"}
    async def customer(shop_id, customer_id):
        return _consenting(marketing_consent=False)
    async def enqueue(**kw):
        rows.append(kw)
        return uuid4()

    monkeypatch.setattr(wq, "get_sender", sender)
    monkeypatch.setattr(wq, "get_template", template)
    monkeypatch.setattr(sms_queries, "get_customer_for_send", customer)
    monkeypatch.setattr(wq, "enqueue", enqueue)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {"1": "Giulia"}}],
        settings=FakeSettings(),
    )

    assert result["queued"] == 0 and result["suppressed"] == 1
    assert rows[0]["status"] == "suppressed"
    assert rows[0]["suppressed_reason"] == "no_consent"


@pytest.mark.asyncio
async def test_enqueue_counts_a_repeat_campaign_as_already_sent(monkeypatch):
    """The unique index is the idempotency: a double click is not two messages."""
    async def sender(shop_id):
        return {"status": "online", "phone_number": "+3721234567", "daily_cap": 50}
    async def template(shop_id, key):
        return {"status": "approved", "content_sid": "HX1"}
    async def customer(shop_id, customer_id):
        return _consenting()
    async def enqueue(**kw):
        return None                       # ON CONFLICT DO NOTHING

    monkeypatch.setattr(wq, "get_sender", sender)
    monkeypatch.setattr(wq, "get_template", template)
    monkeypatch.setattr(sms_queries, "get_customer_for_send", customer)
    monkeypatch.setattr(wq, "enqueue", enqueue)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {"1": "Giulia"}}],
        settings=FakeSettings(),
    )
    assert result["already_sent"] == 1 and result["queued"] == 0


# ----------------------------------------------------------------- the drip

def _patch_send_due(monkeypatch, *, claimed, sender, customer, balance=100000):
    sent, suppressed, failed, deferred, debits = [], [], [], [], []

    async def _claim(limit):
        return claimed
    async def _requeue_stuck(*a, **kw):
        return 0
    async def _get_sender(shop_id):
        return sender
    async def _sent_today(shop_id):
        return sender.get("_sent_today", 0)
    async def _customer(shop_id, customer_id):
        return customer
    async def _balance(shop_id):
        return balance
    async def _debit(**kw):
        debits.append(kw)
        return True
    async def _mark_sent(**kw):
        sent.append(kw)
    async def _mark_suppressed(**kw):
        suppressed.append(kw)
    async def _mark_failed(**kw):
        failed.append(kw)
    async def _requeue_one(**kw):
        deferred.append(kw)

    monkeypatch.setattr(wq, "claim_due", _claim)
    monkeypatch.setattr(wq, "requeue_stuck", _requeue_stuck)
    monkeypatch.setattr(wq, "get_sender", _get_sender)
    monkeypatch.setattr(wq, "sent_today", _sent_today)
    monkeypatch.setattr(sms_queries, "get_customer_for_send", _customer)
    monkeypatch.setattr(tbq, "get_balance", _balance)
    monkeypatch.setattr(tbq, "try_debit_for_message", _debit)
    monkeypatch.setattr(wq, "mark_sent", _mark_sent)
    monkeypatch.setattr(wq, "mark_suppressed", _mark_suppressed)
    monkeypatch.setattr(wq, "mark_failed", _mark_failed)
    monkeypatch.setattr(wq, "requeue_one", _requeue_one)
    return {"sent": sent, "suppressed": suppressed, "failed": failed,
            "deferred": deferred, "debits": debits}


def _message(**over):
    row = {
        "id": uuid4(), "shop_id": SHOP, "customer_id": uuid4(),
        "to_phone": "+393331112222", "from_number": "+3721234567",
        "content_sid": "HX1", "variables": {"1": "Giulia"},
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_send_due_sends_and_debits_after_twilio_accepts(monkeypatch):
    spy = _patch_send_due(
        monkeypatch,
        claimed=[_message()],
        sender={"subaccount_sid": "ACsub", "daily_cap": 50},
        customer=_consenting(),
    )
    monkeypatch.setattr(ws, "_twilio_send", lambda **kw: ("SM1", 0.07))

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["sent"] == 1
    assert spy["sent"][0]["provider_sid"] == "SM1"
    assert spy["debits"][0]["credits"] > 0


@pytest.mark.asyncio
async def test_send_due_rechecks_consent_withdrawn_while_queued(monkeypatch):
    """A row can sit in the queue for hours; consent can change in that window."""
    spy = _patch_send_due(
        monkeypatch,
        claimed=[_message()],
        sender={"subaccount_sid": "ACsub", "daily_cap": 50},
        customer=_consenting(marketing_consent=False),
    )
    def _boom(**kw):
        raise AssertionError("must not reach Twilio")
    monkeypatch.setattr(ws, "_twilio_send", _boom)

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["suppressed"] == 1 and counts["sent"] == 0
    assert spy["suppressed"][0]["reason"] == "no_consent"


@pytest.mark.asyncio
async def test_send_due_defers_rather_than_drops_when_over_cap(monkeypatch):
    """Over the daily cap means later, not never — the owner scheduled it."""
    spy = _patch_send_due(
        monkeypatch,
        claimed=[_message()],
        sender={"subaccount_sid": "ACsub", "daily_cap": 50, "_sent_today": 50},
        customer=_consenting(),
    )
    def _boom(**kw):
        raise AssertionError("must not reach Twilio")
    monkeypatch.setattr(ws, "_twilio_send", _boom)

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["deferred"] == 1 and counts["sent"] == 0
    assert spy["deferred"][0]["minutes"] == 60


@pytest.mark.asyncio
async def test_send_due_stops_at_the_cap_mid_batch(monkeypatch):
    """Three due, two left in today's allowance: two go, one is deferred."""
    spy = _patch_send_due(
        monkeypatch,
        claimed=[_message(), _message(), _message()],
        sender={"subaccount_sid": "ACsub", "daily_cap": 50, "_sent_today": 48},
        customer=_consenting(),
    )
    monkeypatch.setattr(ws, "_twilio_send", lambda **kw: ("SM1", 0.07))

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["sent"] == 2
    assert counts["deferred"] == 1
    assert len(spy["sent"]) == 2


@pytest.mark.asyncio
async def test_send_due_never_bills_for_a_message_twilio_rejected(monkeypatch):
    spy = _patch_send_due(
        monkeypatch,
        claimed=[_message()],
        sender={"subaccount_sid": "ACsub", "daily_cap": 50},
        customer=_consenting(),
    )
    def _reject(**kw):
        raise RuntimeError("63016 template not approved")
    monkeypatch.setattr(ws, "_twilio_send", _reject)

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["failed"] == 1
    assert spy["debits"] == []          # nothing charged
    assert "63016" in spy["failed"][0]["error_code"]


@pytest.mark.asyncio
async def test_send_due_suppresses_when_credits_are_short(monkeypatch):
    spy = _patch_send_due(
        monkeypatch,
        claimed=[_message()],
        sender={"subaccount_sid": "ACsub", "daily_cap": 50},
        customer=_consenting(),
        balance=0,
    )
    def _boom(**kw):
        raise AssertionError("must not reach Twilio")
    monkeypatch.setattr(ws, "_twilio_send", _boom)

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["suppressed"] == 1
    assert spy["suppressed"][0]["reason"] == "insufficient_credits"


@pytest.mark.asyncio
async def test_send_due_decodes_jsonb_variables_returned_as_text(monkeypatch):
    """asyncpg hands jsonb back as a string; Twilio needs the real mapping."""
    seen = {}
    _patch_send_due(
        monkeypatch,
        claimed=[_message(variables='{"1": "Giulia"}')],
        sender={"subaccount_sid": "ACsub", "daily_cap": 50},
        customer=_consenting(),
    )

    def _capture(**kw):
        seen.update(kw)
        return ("SM1", 0.07)
    monkeypatch.setattr(ws, "_twilio_send", _capture)

    await ws.send_due(settings=FakeSettings())

    assert seen["variables"] == {"1": "Giulia"}
