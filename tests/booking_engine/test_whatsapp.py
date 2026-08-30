from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from booking_engine.clients import meta_whatsapp as meta
from booking_engine.db import sms_queries
from booking_engine.db import whatsapp_queries as wq
from booking_engine.services.messaging import meta_limits
from booking_engine.services.messaging import whatsapp_onboarding as wo
from booking_engine.services.messaging import whatsapp_send as ws
from booking_engine.services.messaging import whatsapp_templates as wt

SHOP = uuid4()
ROME = ZoneInfo("Europe/Rome")


class FakeSettings:
    public_base_url = "https://example.test"
    whatsapp_send_start_hour = 9
    whatsapp_send_end_hour = 20
    # 0 disables pacing: the scheduler is tested on its own in test_pacer.py,
    # and real sleeps would make every send test slow for no extra coverage.
    whatsapp_sends_per_minute = 0
    whatsapp_recipient_cooldown_hours = 168
    meta_access_verified = False
    meta_app_id = "app"
    meta_app_secret = "secret"
    meta_config_id = "cfg"
    meta_solution_id = "sol"
    meta_verify_token = "verify"
    meta_kairo_waba_id = "KAIRO_WABA"
    meta_kairo_token = "kairo-token"


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


# ------------------------------------------------- scheduling: bulk, multi-day

def test_spread_rolls_a_bulk_campaign_onto_following_days():
    """400 recipients against a 50/day cap is eight days of drip, not one.

    Laying them all on today would just hand send_due 350 rows to defer by an
    hour, repeatedly, until nobody can read the queue.
    """
    now = datetime(2026, 8, 20, 9, 0, tzinfo=ROME)
    times = ws.spread(400, now, start_hour=9, end_hour=20, daily_cap=50)

    assert len(times) == 400
    assert times == sorted(times)
    per_day = {}
    for when in times:
        per_day[when.date()] = per_day.get(when.date(), 0) + 1
    assert len(per_day) == 8
    assert set(per_day.values()) == {50}


def test_spread_respects_the_cap_on_a_partial_first_day():
    """Enqueued at 18:00: today still takes a full day's worth, then rolls."""
    now = datetime(2026, 8, 20, 18, 0, tzinfo=ROME)
    times = ws.spread(70, now, start_hour=9, end_hour=20, daily_cap=50)

    today = [t for t in times if t.date() == now.date()]
    assert len(today) == 50
    assert all(t < datetime(2026, 8, 20, 20, 0, tzinfo=ROME) for t in today)
    assert len(times) == 70


def test_spread_skips_a_day_whose_window_has_already_closed():
    """Enqueued at 23:00 the whole campaign starts tomorrow, not at 23:00."""
    now = datetime(2026, 8, 20, 23, 0, tzinfo=ROME)
    times = ws.spread(60, now, start_hour=9, end_hour=20, daily_cap=50)

    assert times[0] == datetime(2026, 8, 21, 9, 0, tzinfo=ROME)
    assert {t.date() for t in times} == {
        datetime(2026, 8, 21).date(), datetime(2026, 8, 22).date()
    }


def test_spread_never_schedules_outside_opening_hours():
    now = datetime(2026, 8, 20, 9, 0, tzinfo=ROME)
    for when in ws.spread(300, now, start_hour=9, end_hour=20, daily_cap=50):
        assert 9 <= when.hour < 20


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

def _online_sender(**over):
    row = {"status": "online", "phone_number": "+393331110000",
           "phone_number_id": "PN1", "access_token": "tok", "daily_cap": 50,
           "messaging_limit": "TIER_1K", "platform_type": "COEXISTENCE"}
    row.update(over)
    return row


def _approved_template(**over):
    row = {"status": "approved", "name": "kairo_promo_v1", "language": "it"}
    row.update(over)
    return row


def _patch_enqueue(monkeypatch, *, sender=None, template=None, cooled=()):
    async def _sender(shop_id):
        return sender if sender is not None else _online_sender()
    async def _template(shop_id, key):
        return template if template is not None else _approved_template()
    async def _recent(*, shop_id, customer_ids, hours):
        return {c for c in customer_ids if c in cooled}
    monkeypatch.setattr(wq, "get_sender", _sender)
    monkeypatch.setattr(wq, "get_template", _template)
    monkeypatch.setattr(wq, "recently_contacted", _recent)


@pytest.mark.asyncio
async def test_enqueue_refuses_when_the_sender_is_not_online(monkeypatch):
    _patch_enqueue(monkeypatch, sender={"status": "verifying"})

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}}],
        settings=FakeSettings(),
    )
    assert result == {"ok": False, "error": "sender_not_online"}


@pytest.mark.asyncio
async def test_enqueue_refuses_an_unapproved_template(monkeypatch):
    _patch_enqueue(monkeypatch, template=_approved_template(status="rejected"))

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}}],
        settings=FakeSettings(),
    )
    assert result["error"] == "template_rejected"


@pytest.mark.asyncio
async def test_enqueue_spreads_past_the_daily_cap_instead_of_refusing(monkeypatch):
    """The old `over_daily_cap` rejection made bulk impossible.

    A campaign larger than one day's allowance is now a multi-day drip; only
    the plan's monthly allowance is a hard ceiling.
    """
    rows = []
    _patch_enqueue(monkeypatch)

    async def customer(shop_id, customer_id):
        return _consenting()
    async def enqueue(**kw):
        rows.append(kw)
        return uuid4()
    monkeypatch.setattr(sms_queries, "get_customer_for_send", customer)
    monkeypatch.setattr(wq, "enqueue", enqueue)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}} for _ in range(120)],
        settings=FakeSettings(),
    )

    assert result["ok"] is True and result["queued"] == 120
    scheduled = [r["scheduled_at"] for r in rows]
    assert len({s.date() for s in scheduled}) == 3      # 120 / 50 -> 3 days
    assert result["last_at"] > result["first_at"]


@pytest.mark.asyncio
async def test_enqueue_records_a_suppressed_row_for_no_consent(monkeypatch):
    """A refusal is a row, never silence: 'why did Giulia not get it?'"""
    rows = []
    _patch_enqueue(monkeypatch)

    async def customer(shop_id, customer_id):
        return _consenting(marketing_consent=False)
    async def enqueue(**kw):
        rows.append(kw)
        return uuid4()
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
async def test_enqueue_writes_the_template_name_meta_sends_by(monkeypatch):
    """Meta addresses a template by name + language, never by an opaque id."""
    rows = []
    _patch_enqueue(monkeypatch)

    async def customer(shop_id, customer_id):
        return _consenting()
    async def enqueue(**kw):
        rows.append(kw)
        return uuid4()
    monkeypatch.setattr(sms_queries, "get_customer_for_send", customer)
    monkeypatch.setattr(wq, "enqueue", enqueue)

    await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {"1": "Giulia"}}],
        settings=FakeSettings(),
    )
    assert rows[0]["template_name"] == "kairo_promo_v1"
    assert rows[0]["template_language"] == "it"


@pytest.mark.asyncio
async def test_enqueue_counts_a_repeat_campaign_as_already_sent(monkeypatch):
    """The unique index is the idempotency: a double click is not two messages."""
    _patch_enqueue(monkeypatch)

    async def customer(shop_id, customer_id):
        return _consenting()
    async def enqueue(**kw):
        return None                       # ON CONFLICT DO NOTHING
    monkeypatch.setattr(sms_queries, "get_customer_for_send", customer)
    monkeypatch.setattr(wq, "enqueue", enqueue)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {"1": "Giulia"}}],
        settings=FakeSettings(),
    )
    assert result["already_sent"] == 1 and result["queued"] == 0


# ----------------------------------------------------------------- the drip

def _patch_send_due(
    monkeypatch, *, claimed, sender, customer,
    cooled=(),
):
    spy = {"sent": [], "suppressed": [], "failed": [], "deferred": [],
           "consent_withdrawn": []}

    async def _claim(limit):
        return claimed
    async def _requeue_stuck(*a, **kw):
        return 0
    async def _get_sender(shop_id):
        return sender
    async def _sent_24h(shop_id):
        return sender.get("_sent_today", 0)
    async def _recent(*, shop_id, customer_ids, hours):
        return {c for c in customer_ids if c in cooled}
    async def _customer(shop_id, customer_id):
        return customer
    async def _mark_sent(**kw):
        spy["sent"].append(kw)
    async def _mark_suppressed(**kw):
        spy["suppressed"].append(kw)
    async def _mark_failed(**kw):
        spy["failed"].append(kw)
    async def _requeue_one(**kw):
        spy["deferred"].append(kw)
    async def _withdraw(customer_id):
        spy["consent_withdrawn"].append(customer_id)

    monkeypatch.setattr(wq, "claim_due", _claim)
    monkeypatch.setattr(wq, "requeue_stuck", _requeue_stuck)
    monkeypatch.setattr(wq, "get_sender", _get_sender)
    monkeypatch.setattr(wq, "sent_last_24h", _sent_24h)
    monkeypatch.setattr(wq, "recently_contacted", _recent)
    monkeypatch.setattr(sms_queries, "get_customer_for_send", _customer)
    monkeypatch.setattr(wq, "mark_sent", _mark_sent)
    monkeypatch.setattr(wq, "mark_suppressed", _mark_suppressed)
    monkeypatch.setattr(wq, "mark_failed", _mark_failed)
    monkeypatch.setattr(wq, "requeue_one", _requeue_one)
    monkeypatch.setattr(wq, "withdraw_marketing_consent", _withdraw)
    return spy


def _patch_meta_send(monkeypatch, fn):
    monkeypatch.setattr(meta, "send_template", fn)


def _ok_send(wamid="wamid.1"):
    async def _send(**kw):
        return wamid
    return _send


def _never_sends():
    async def _send(**kw):
        raise AssertionError("must not reach Meta")
    return _send


def _message(**over):
    row = {
        "id": uuid4(), "shop_id": SHOP, "customer_id": uuid4(),
        "to_phone": "+393331112222", "from_number": "+393331110000",
        "template_name": "kairo_promo_v1", "template_language": "it",
        "variables": {"1": "Giulia"},
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_send_due_sends_via_meta_and_records_the_wamid(monkeypatch):
    spy = _patch_send_due(
        monkeypatch, claimed=[_message()],
        sender=_online_sender(), customer=_consenting(),
    )
    _patch_meta_send(monkeypatch, _ok_send("wamid.abc"))

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["sent"] == 1
    assert spy["sent"][0]["provider_sid"] == "wamid.abc"


@pytest.mark.asyncio
async def test_send_due_never_debits_credits(monkeypatch):
    """The salon's card is on the salon's WABA — Meta bills it, not us.

    Debiting here would charge the same message twice. Guarded by asserting
    the send path records no credits at all, rather than trusting that nobody
    re-adds the import later.
    """
    spy = _patch_send_due(
        monkeypatch, claimed=[_message()],
        sender=_online_sender(), customer=_consenting(),
    )
    _patch_meta_send(monkeypatch, _ok_send())

    await ws.send_due(settings=FakeSettings())

    assert spy["sent"][0]["credits"] is None
    assert spy["sent"][0]["price_usd"] > 0        # still quoted, never charged


@pytest.mark.asyncio
async def test_send_due_rechecks_consent_withdrawn_while_queued(monkeypatch):
    """A row can sit in the queue for days now; consent can change in between."""
    spy = _patch_send_due(
        monkeypatch, claimed=[_message()],
        sender=_online_sender(), customer=_consenting(marketing_consent=False),
    )
    _patch_meta_send(monkeypatch, _never_sends())

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["suppressed"] == 1 and counts["sent"] == 0
    assert spy["suppressed"][0]["reason"] == "no_consent"


@pytest.mark.asyncio
async def test_send_due_defers_rather_than_drops_when_over_cap(monkeypatch):
    """Over the daily cap means later, not never — the owner scheduled it."""
    spy = _patch_send_due(
        monkeypatch, claimed=[_message()],
        sender=_online_sender(_sent_today=50), customer=_consenting(),
    )
    _patch_meta_send(monkeypatch, _never_sends())

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["deferred"] == 1 and counts["sent"] == 0
    assert spy["deferred"][0]["minutes"] == 60


@pytest.mark.asyncio
async def test_send_due_stops_at_the_cap_mid_batch(monkeypatch):
    """Three due, two left in today's allowance: two go, one is deferred."""
    spy = _patch_send_due(
        monkeypatch, claimed=[_message(), _message(), _message()],
        sender=_online_sender(_sent_today=48), customer=_consenting(),
    )
    _patch_meta_send(monkeypatch, _ok_send())

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["sent"] == 2
    assert counts["deferred"] == 1
    assert len(spy["sent"]) == 2


@pytest.mark.asyncio
async def test_send_due_marks_an_unknown_meta_error_failed(monkeypatch):
    spy = _patch_send_due(
        monkeypatch, claimed=[_message()],
        sender=_online_sender(), customer=_consenting(),
    )

    async def _reject(**kw):
        raise meta.MetaError(132000, "template param count mismatch")
    _patch_meta_send(monkeypatch, _reject)

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["failed"] == 1
    assert "132000" in spy["failed"][0]["error_code"]
    assert spy["consent_withdrawn"] == []


@pytest.mark.asyncio
async def test_send_due_treats_131050_as_a_permanent_opt_out(monkeypatch):
    """Meta's native "Stop promotions" button — the opt-out SMS never had."""
    message = _message()
    spy = _patch_send_due(
        monkeypatch, claimed=[message],
        sender=_online_sender(), customer=_consenting(),
    )

    async def _opted_out(**kw):
        raise meta.MetaError(131050, "user stopped marketing messages")
    _patch_meta_send(monkeypatch, _opted_out)

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["suppressed"] == 1
    assert spy["suppressed"][0]["reason"] == "opted_out"
    assert spy["consent_withdrawn"] == [message["customer_id"]]
    assert spy["deferred"] == []


@pytest.mark.asyncio
async def test_send_due_treats_131049_as_a_cooldown_not_an_opt_out(monkeypatch):
    """The cross-brand frequency cap says "not today", not "never again".

    Collapsing it into the opt-out branch — as the Twilio version's single
    bucket would have — permanently silences customers who did nothing.
    """
    spy = _patch_send_due(
        monkeypatch, claimed=[_message()],
        sender=_online_sender(), customer=_consenting(),
    )

    async def _capped(**kw):
        raise meta.MetaError(131049, "healthy ecosystem engagement")
    _patch_meta_send(monkeypatch, _capped)

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["rate_capped"] == 1
    assert counts["suppressed"] == 0 and counts["failed"] == 0
    assert spy["consent_withdrawn"] == []
    assert spy["deferred"][0]["minutes"] == ws.FREQUENCY_CAP_RETRY_MINUTES


@pytest.mark.asyncio
async def test_send_due_decodes_jsonb_variables_returned_as_text(monkeypatch):
    """asyncpg hands jsonb back as a string; Meta needs the real mapping."""
    seen = {}
    _patch_send_due(
        monkeypatch, claimed=[_message(variables='{"1": "Giulia"}')],
        sender=_online_sender(), customer=_consenting(),
    )

    async def _capture(**kw):
        seen.update(kw)
        return "wamid.1"
    _patch_meta_send(monkeypatch, _capture)

    await ws.send_due(settings=FakeSettings())

    assert seen["variables"] == {"1": "Giulia"}
    assert seen["name"] == "kairo_promo_v1"
    assert seen["language"] == "it"


# ------------------------------------------------------------- plan quota

@pytest.mark.asyncio
async def test_enqueue_has_no_kairo_side_monthly_allowance(monkeypatch):
    """The plan quota was removed on 2026-08-24 and must not come back.

    Under the Meta Tech Provider model the salon's own card is on their own
    WABA, so a Kairo-side ceiling recovers no cost of ours and only suppresses
    the usage that makes the product stick. Meta's tier is the real limit, it
    is enforced separately (effective_daily_cap), and it is one we can read.

    A campaign far larger than any plan allowance ever was must queue.
    """
    _patch_enqueue(monkeypatch)

    async def customer(shop_id, customer_id):
        return _consenting()
    async def enqueue(**kw):
        return uuid4()
    monkeypatch.setattr(sms_queries, "get_customer_for_send", customer)
    monkeypatch.setattr(wq, "enqueue", enqueue)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}} for _ in range(200)],
        settings=FakeSettings(),
    )

    assert result["ok"] is True
    assert "over_monthly_quota" not in str(result)


# ------------------------------------------------------------------ onboarding

def _patch_onboarding(monkeypatch, *, sender, calls):
    async def _get_sender(shop_id):
        return sender
    async def _set_fields(shop_id, **fields):
        calls.setdefault("fields", []).append(fields)
        sender.update(fields)
    async def _get_template(shop_id, key):
        return None
    async def _upsert_template(**kw):
        calls.setdefault("templates", []).append(kw)
        return kw
    async def _onboarded(*a, **kw):
        return calls.get("onboarded_last_7_days", 0)
    monkeypatch.setattr(wq, "get_sender", _get_sender)
    monkeypatch.setattr(wq, "set_sender_fields", _set_fields)
    monkeypatch.setattr(wq, "get_template", _get_template)
    monkeypatch.setattr(wq, "upsert_template", _upsert_template)
    monkeypatch.setattr(wq, "onboarded_last_7_days", _onboarded)

    async def _exchange(**kw):
        calls.setdefault("exchange", []).append(kw)
        return "customer-token"
    async def _subscribe(**kw):
        calls.setdefault("subscribe", []).append(kw)
    async def _number(**kw):
        return meta.PhoneNumber(
            id="PN1", display_phone_number="+393331110000",
            verified_name="Salone X", quality_rating="GREEN",
            messaging_limit="TIER_1K", throughput_level="STANDARD",
            platform_type="COEXISTENCE", is_on_biz_app=True,
        )
    async def _create_template(**kw):
        calls.setdefault("create_template", []).append(kw)
        return "TPL1", "pending"
    async def _fetch_template(**kw):
        calls.setdefault("fetch_template", []).append(kw)
        return meta.TemplateStatus(status="approved", rejection_reason=None)
    monkeypatch.setattr(meta, "exchange_code", _exchange)
    monkeypatch.setattr(meta, "subscribe_app", _subscribe)
    monkeypatch.setattr(meta, "get_phone_number", _number)
    monkeypatch.setattr(meta, "create_template", _create_template)
    monkeypatch.setattr(meta, "fetch_template", _fetch_template)
    return calls


@pytest.mark.asyncio
async def test_complete_onboards_coexistence_in_one_round_trip(monkeypatch):
    """No OTP, no subaccount, no second call: the popup already verified."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={},
    )

    result = await wo.complete(
        shop_id=SHOP, code="c0de", waba_id="WABA1", phone_number_id="PN1",
        settings=FakeSettings(),
    )

    assert result["ok"] is True and result["status"] == "online"
    assert result["coexistence"] is True
    assert calls["subscribe"][0]["waba_id"] == "WABA1"


@pytest.mark.asyncio
async def test_complete_subscribes_to_webhooks_before_reading_the_number(monkeypatch):
    """Without the subscription every send succeeds and we hear nothing back.

    No delivery status, no template verdicts, no opt-outs — broken in the one
    way nothing surfaces, so ordering is asserted rather than assumed.
    """
    order = []
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={},
    )

    async def _subscribe(**kw):
        order.append("subscribe")
    async def _number(**kw):
        order.append("get_phone_number")
        return meta.PhoneNumber(
            id="PN1", display_phone_number="+393331110000",
            verified_name="Salone X", quality_rating="GREEN",
            messaging_limit="TIER_1K", throughput_level="STANDARD",
            platform_type="COEXISTENCE", is_on_biz_app=True,
        )
    monkeypatch.setattr(meta, "subscribe_app", _subscribe)
    monkeypatch.setattr(meta, "get_phone_number", _number)

    await wo.complete(shop_id=SHOP, code="c0de", waba_id="W", phone_number_id="P",
                      settings=FakeSettings())

    assert order == ["subscribe", "get_phone_number"]
    del calls


@pytest.mark.asyncio
async def test_complete_persists_the_token_before_using_it(monkeypatch):
    """A crash after the exchange must leave a resumable row.

    Losing the token would leave a WABA we are subscribed to and can neither
    reach nor unsubscribe from.
    """
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={},
    )

    async def _boom(**kw):
        raise meta.MetaError(100, "subscribe failed")
    monkeypatch.setattr(meta, "subscribe_app", _boom)

    result = await wo.complete(
        shop_id=SHOP, code="c0de", waba_id="WABA1", phone_number_id="PN1",
        settings=FakeSettings(),
    )

    assert result["ok"] is False
    token_writes = [f for f in calls["fields"] if f.get("access_token")]
    assert token_writes and token_writes[0]["access_token"] == "customer-token"


@pytest.mark.asyncio
async def test_complete_injects_the_catalogue_into_the_salons_waba(monkeypatch):
    """The call Twilio structurally could not make — the point of the migration."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={},
    )

    await wo.complete(shop_id=SHOP, code="c0de", waba_id="WABA1",
                      phone_number_id="PN1", settings=FakeSettings())

    created = calls["create_template"]
    assert {c["name"] for c in created} == {
        wo.template_name(k) for k in wt.CATALOGUE
    }
    assert all(c["waba_id"] == "WABA1" for c in created)
    assert all(c["token"] == "customer-token" for c in created)


@pytest.mark.asyncio
async def test_ensure_templates_survives_one_rejected_template(monkeypatch):
    """One bad template must not abort the rest of the catalogue."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )

    async def _reject(**kw):
        raise meta.MetaError(2388042, "invalid parameter")
    monkeypatch.setattr(meta, "create_template", _reject)

    result = await wo.ensure_templates(shop_id=SHOP, settings=FakeSettings())

    assert result["ok"] is True
    assert result["created"] == 0
    assert set(result["failed"]) == set(wt.CATALOGUE)
    del calls


@pytest.mark.asyncio
async def test_ensure_templates_skips_a_template_not_yet_approved_on_kairo_waba(monkeypatch):
    """Test on Kairo's own WABA first; a customer WABA only sees what passed."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )

    async def _pending(**kw):
        return meta.TemplateStatus(status="pending", rejection_reason=None)
    monkeypatch.setattr(meta, "fetch_template", _pending)

    result = await wo.ensure_templates(shop_id=SHOP, settings=FakeSettings())

    assert result["created"] == 0
    assert set(result["not_ready"]) == set(wt.CATALOGUE)
    assert "create_template" not in calls


@pytest.mark.asyncio
async def test_ensure_templates_fails_closed_without_kairo_waba_configured(monkeypatch):
    """No Kairo WABA set up yet means nothing propagates — not "propagate unchecked"."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )

    class NoKairoWaba(FakeSettings):
        meta_kairo_waba_id = ""
        meta_kairo_token = ""

    result = await wo.ensure_templates(shop_id=SHOP, settings=NoKairoWaba())

    assert result["created"] == 0
    assert set(result["not_ready"]) == set(wt.CATALOGUE)
    assert "create_template" not in calls
    assert "fetch_template" not in calls


def test_signup_config_asks_meta_for_the_coexistence_branch():
    """Without this flag the popup offers only a brand-new WABA.

    The salon would be told to delete their WhatsApp Business App — exactly
    the Twilio behaviour this migration exists to avoid.
    """
    config = wo.signup_config(FakeSettings())
    assert config["feature_type"] == "whatsapp_business_app_onboarding"
    assert config["config_id"] == "cfg"
    assert config["solution_id"] == "sol"


def test_template_names_are_stable_across_shops():
    """Meta scopes names per-WABA, so every salon carries the same one."""
    assert wo.template_name("promo_v1") == "kairo_promo_v1"


def test_unknown_meta_template_status_is_never_treated_as_approved():
    """A new Meta state must not silently make a template sendable."""
    assert wo.TEMPLATE_STATUS.get("some_future_state", "pending") == "pending"
    assert wo.TEMPLATE_STATUS["approved"] == "approved"


@pytest.mark.asyncio
async def test_send_due_defers_rather_than_burns_a_sender_missing_credentials(monkeypatch):
    """A config problem must not permanently fail the owner's messages.

    Without the guard the missing column reaches the send as a KeyError, gets
    caught as a provider error, and marks the row `failed` forever.
    """
    spy = _patch_send_due(
        monkeypatch, claimed=[_message()],
        sender=_online_sender(access_token=None), customer=_consenting(),
    )
    _patch_meta_send(monkeypatch, _never_sends())

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["deferred"] == 1
    assert counts["failed"] == 0 and spy["failed"] == []


# ------------------------------------------------------- Meta limit safeguards

@pytest.mark.asyncio
async def test_enqueue_uses_metas_tier_when_our_cap_is_set_higher(monkeypatch):
    """A daily_cap of 5000 on a Tier-250 sender must schedule at 250/day.

    The commercial knob may only narrow the platform ceiling. Before this,
    `daily_cap` was an unrelated hand-set number and nothing read the tier.
    """
    rows = []
    _patch_enqueue(monkeypatch, sender=_online_sender(
        daily_cap=5000, messaging_limit="TIER_250"))

    async def customer(shop_id, customer_id):
        return _consenting()
    async def enqueue(**kw):
        rows.append(kw)
        return uuid4()
    monkeypatch.setattr(sms_queries, "get_customer_for_send", customer)
    monkeypatch.setattr(wq, "enqueue", enqueue)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}} for _ in range(600)],
        settings=FakeSettings(),
    )

    assert result["ok"] is True
    per_day = {}
    for row in rows:
        d = row["scheduled_at"].date()
        per_day[d] = per_day.get(d, 0) + 1
    assert max(per_day.values()) == 250          # Meta's tier, not our 5000


@pytest.mark.asyncio
async def test_enqueue_falls_back_to_the_unverified_floor_for_an_unknown_tier(monkeypatch):
    """Meta telling us something we don't recognise must not widen anything."""
    rows = []
    _patch_enqueue(monkeypatch, sender=_online_sender(
        daily_cap=5000, messaging_limit="TIER_5K"))

    async def customer(shop_id, customer_id):
        return _consenting()
    async def enqueue(**kw):
        rows.append(kw)
        return uuid4()
    monkeypatch.setattr(sms_queries, "get_customer_for_send", customer)
    monkeypatch.setattr(wq, "enqueue", enqueue)

    await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}} for _ in range(400)],
        settings=FakeSettings(),
    )
    first_day = rows[0]["scheduled_at"].date()
    assert sum(1 for r in rows if r["scheduled_at"].date() == first_day) == 250


@pytest.mark.asyncio
async def test_enqueue_suppresses_a_customer_inside_the_cooldown(monkeypatch):
    """Our guard against Meta's per-user cross-brand cap, applied *before* the send.

    Reacting to 131049 costs the send and a quality-rating hit; not sending is
    free.
    """
    rows = []
    cooled = uuid4()
    fresh = uuid4()
    _patch_enqueue(monkeypatch, cooled={cooled})

    async def customer(shop_id, customer_id):
        return _consenting()
    async def enqueue(**kw):
        rows.append(kw)
        return uuid4()
    monkeypatch.setattr(sms_queries, "get_customer_for_send", customer)
    monkeypatch.setattr(wq, "enqueue", enqueue)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": cooled, "variables": {}},
                    {"customer_id": fresh, "variables": {}}],
        settings=FakeSettings(),
    )

    assert result["queued"] == 1 and result["suppressed"] == 1
    by_customer = {r["customer_id"]: r for r in rows}
    assert by_customer[cooled]["suppressed_reason"] == "recently_contacted"
    assert by_customer[fresh]["suppressed_reason"] is None


@pytest.mark.asyncio
async def test_enqueue_refuses_a_us_recipient_before_meta_does(monkeypatch):
    """Meta has not delivered marketing to +1 since 2025-04-01."""
    rows = []
    _patch_enqueue(monkeypatch)

    async def customer(shop_id, customer_id):
        return _consenting(phone="+12125550123")
    async def enqueue(**kw):
        rows.append(kw)
        return uuid4()
    monkeypatch.setattr(sms_queries, "get_customer_for_send", customer)
    monkeypatch.setattr(wq, "enqueue", enqueue)

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}}],
        settings=FakeSettings(),
    )

    assert result["suppressed"] == 1 and result["queued"] == 0
    assert rows[0]["suppressed_reason"] == "marketing_blocked_destination"


@pytest.mark.asyncio
async def test_enqueue_refuses_a_sender_with_no_allowance_at_all(monkeypatch):
    _patch_enqueue(monkeypatch, sender=_online_sender(daily_cap=0))

    result = await ws.enqueue_campaign(
        shop_id=SHOP, campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {}}],
        settings=FakeSettings(),
    )
    assert result == {"ok": False, "error": "sender_has_no_allowance"}


@pytest.mark.asyncio
async def test_send_due_counts_metas_rolling_window_not_the_calendar_day(monkeypatch):
    """The tier is measured over a rolling 24h, so the check must be too.

    A calendar-day count resets at midnight and would hand a sender sitting at
    its ceiling a second full allowance ninety minutes later — nearly two
    tiers' worth of traffic inside one of Meta's windows.
    """
    seen = {}
    spy = _patch_send_due(
        monkeypatch, claimed=[_message()],
        sender=_online_sender(_sent_today=50), customer=_consenting(),
    )

    async def _calendar_day(shop_id):
        seen["calendar_day_used"] = True
        return 0
    monkeypatch.setattr(wq, "sent_today", _calendar_day)
    _patch_meta_send(monkeypatch, _never_sends())

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["deferred"] == 1               # the rolling count bound it
    assert "calendar_day_used" not in seen       # and sent_today was never asked
    del spy


@pytest.mark.asyncio
async def test_send_due_clamps_the_configured_rate_to_metas_throughput(monkeypatch):
    """A misconfigured WHATSAPP_SENDS_PER_MINUTE must not be able to burst.

    Asserted on the Pacer the loop actually builds, so raising the env var to
    something absurd cannot silently remove the ceiling.
    """
    built = {}
    real_pacer = ws.Pacer

    class SpyPacer(real_pacer):
        def __init__(self, per_minute):
            built["per_minute"] = per_minute
            super().__init__(per_minute)

    monkeypatch.setattr(ws, "Pacer", SpyPacer)
    _patch_send_due(monkeypatch, claimed=[_message()],
                    sender=_online_sender(), customer=_consenting())
    _patch_meta_send(monkeypatch, _ok_send())

    class Absurd(FakeSettings):
        whatsapp_sends_per_minute = 10_000

    await ws.send_due(settings=Absurd())

    assert built["per_minute"] == meta_limits.MAX_SENDS_PER_MINUTE


@pytest.mark.asyncio
async def test_send_due_rechecks_the_cooldown_for_a_row_queued_days_ago(monkeypatch):
    """A multi-day drip can be overtaken by another campaign in the meantime."""
    message = _message()
    spy = _patch_send_due(
        monkeypatch, claimed=[message],
        sender=_online_sender(), customer=_consenting(),
        cooled={message["customer_id"]},
    )
    _patch_meta_send(monkeypatch, _never_sends())

    counts = await ws.send_due(settings=FakeSettings())

    assert counts["suppressed"] == 1
    assert spy["suppressed"][0]["reason"] == "recently_contacted"


@pytest.mark.asyncio
async def test_complete_refuses_past_metas_onboarding_limit(monkeypatch):
    """10 new customers per rolling 7 days until Access Verification.

    The 11th otherwise fails at Meta with an opaque error, after the popup's
    single-use code has already been spent.
    """
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={"onboarded_last_7_days": 10},
    )

    result = await wo.complete(
        shop_id=SHOP, code="c0de", waba_id="WABA1", phone_number_id="PN1",
        settings=FakeSettings(),
    )

    assert result["ok"] is False
    assert result["error"] == "onboarding_limit_reached"
    assert result["limit"] == 10
    assert "exchange" not in calls          # the code was not spent


@pytest.mark.asyncio
async def test_complete_allows_more_once_access_verification_is_done(monkeypatch):
    _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={"onboarded_last_7_days": 10},
    )

    class Verified(FakeSettings):
        meta_access_verified = True

    result = await wo.complete(
        shop_id=SHOP, code="c0de", waba_id="WABA1", phone_number_id="PN1",
        settings=Verified(),
    )
    assert result["ok"] is True


# ------------------------------------------------- marketing-only counting

def test_owner_counters_and_the_cooldown_count_marketing_only():
    """The change with no failure mode: nothing breaks, the numbers just lie.

    A utility template (an appointment reminder) must not appear in the
    owner's campaign counter, and must not block next week's promotion. This
    asserts on the SQL text because the filter is pure SQL — there is nothing
    to monkeypatch and no assertion a mocked DB could make.
    """
    import inspect
    from booking_engine.db import whatsapp_queries as q

    for fn in (q.sent_today, q.sent_this_month, q.recently_contacted):
        source = inspect.getsource(fn)
        assert "_MARKETING_JOIN" in source, (
            f"{fn.__name__} counts every send, including reminders"
        )

    assert "category = 'MARKETING'" in q._MARKETING_JOIN
    # Joined on the name, because migration 15 dropped content_sid.
    assert "t.name = om.template_name" in q._MARKETING_JOIN


def test_metas_tier_window_counts_every_business_initiated_message():
    """The other half, and the one that must NOT be narrowed.

    Meta's messaging-limit tier counts every business-initiated conversation,
    utility included. Filtering this to marketing would let a salon send its
    marketing allowance *on top of* its reminders and blow through the tier.
    """
    import inspect
    from booking_engine.db import whatsapp_queries as q

    assert "_MARKETING_JOIN" not in inspect.getsource(q.sent_last_24h)


@pytest.mark.asyncio
async def test_webhook_persists_inbound_replies(monkeypatch):
    """Campaign measurement needs "did this recipient reply within 72h" as a
    queryable signal, so the webhook must store a reply, not just log it.

    The reply is matched back to the message it answers by phone: the reply's
    `from_phone` equals the sent message's `to_phone`. Only the phone, body and
    shop travel — no sender identity is required or expected here.
    """
    from booking_engine.api.routes import whatsapp as wa_routes

    captured = {}
    async def _fake_record_inbound(**kw):
        captured.update(kw)
    monkeypatch.setattr(wa_routes.wq, "record_inbound", _fake_record_inbound)

    await wa_routes._handle_change(
        sender={"shop_id": SHOP},
        change={
            "field": "messages",
            "value": {
                "messages": [{
                    "from": "+393331112222",
                    "type": "text",
                    "text": {"body": "Certo, prenoto per giovedì!"},
                }],
            },
        },
    )

    assert captured["shop_id"] == SHOP
    assert captured["from_phone"] == "+393331112222"
    assert captured["body"] == "Certo, prenoto per giovedì!"
    assert captured["message_type"] == "text"
