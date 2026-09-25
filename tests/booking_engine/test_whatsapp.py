from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from booking_engine.api.routes import whatsapp as whatsapp_routes
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
    meta_receipt_sample_url = "https://example.test/sample.pdf"


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
    text = wt.render("promo_v1", {
        "1": "Giulia", "2": "Chiara", "3": "Salone X",
        "4": "Sono passate circa tre settimane dal tuo colore: com'è la ricrescita?",
    })
    assert "Ciao Giulia," in text
    assert "sono Chiara di Salone X" in text
    assert "{{" not in text


def test_every_catalogue_template_has_a_sample_for_each_variable():
    """Meta rejects a body starting with a variable unless a sample is sent."""
    for key, tpl in wt.CATALOGUE.items():
        for n in range(1, tpl.variables + 1):
            assert str(n) in tpl.sample, f"{key} is missing a sample for {{{{{n}}}}}"


def test_rebook_never_mentions_money():
    """Owner rule: rebook_v1 is about services, never amounts. Meta approves
    bodies, so the taboo has to hold in the fixed text AND in the sample (what
    Meta sees) AND in the guidance (what the LLM reads)."""
    tpl = wt.CATALOGUE["rebook_v1"]
    blob = (tpl.body + tpl.guidance + " ".join(tpl.sample.values())).lower()
    for token in ("€", "eur", "euro", "prezzo", "sconto"):
        assert token not in blob, f"rebook_v1 must not mention money: {token!r}"


def test_marketing_templates_carry_the_soft_cta_tail():
    """The shared low-pressure close is part of the approved body, not left to
    the LLM — a hard-sell imperative reads worse and is harder to get Meta to
    approve. promo_manual_v1 is owner copy with its own approved frame, so it
    is deliberately excluded."""
    for key in ("promo_v1", "winback_v1", "rebook_v1"):
        assert "Se ti va, scrivimi pure." in wt.CATALOGUE[key].body, key
    for key in ("promo_manual_v1", "feedback_v2", "reminder_v6"):
        assert "Se ti va, scrivimi pure." not in wt.CATALOGUE[key].body, key


# --------------------------------------------------------------------- gating

def _online_sender(**over):
    row = {"status": "online", "phone_number": "+393331110000",
           "phone_number_id": "PN1", "access_token": "tok", "daily_cap": 50,
           "messaging_limit": "TIER_1K", "platform_type": "COEXISTENCE"}
    row.update(over)
    return row


def _approved_template(**over):
    row = {"status": "approved", "name": "it_promo_v1", "language": "it"}
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
    # The name is read off the shop's own template row, not composed here: the
    # send must address exactly what was injected into that WABA.
    assert rows[0]["template_name"] == "it_promo_v1"
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

    async def _claim(limit, **kw):
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
        # `calls["template"]` lets a test give every key the same existing row —
        # enough to exercise the drift/edit and under-review paths.
        return calls.get("template")
    async def _upsert_template(**kw):
        calls.setdefault("templates", []).append(kw)
        return kw
    async def _onboarded(*a, **kw):
        return calls.get("onboarded_last_7_days", 0)
    # The shop's platform locale — what template names are composed from.
    # `calls["language"]` lets a test run a shop on another locale.
    async def _language(shop_id):
        return calls.get("language", "it")
    monkeypatch.setattr(wq, "get_shop_language", _language)
    monkeypatch.setattr(wq, "get_sender", _get_sender)
    monkeypatch.setattr(wq, "set_sender_fields", _set_fields)
    monkeypatch.setattr(wq, "get_template", _get_template)
    monkeypatch.setattr(wq, "upsert_template", _upsert_template)
    monkeypatch.setattr(wq, "onboarded_last_7_days", _onboarded)
    # The sweep's last stage. Stubbed here rather than in each sweep test:
    # left live it reaches for a real pool, which is a confusing way for an
    # unrelated test to fail.
    async def _due_for_reminder(**kw):
        return calls.get("token_reminders", [])
    async def _mark_reminded(shop_id):
        calls.setdefault("reminded", []).append(shop_id)
    monkeypatch.setattr(wq, "list_senders_needing_token_reminder", _due_for_reminder)
    monkeypatch.setattr(wq, "mark_token_reminder_sent", _mark_reminded)

    async def _exchange(**kw):
        calls.setdefault("exchange", []).append(kw)
        # Our Login Configuration mints 60-day tokens, so the expiring shape
        # is the production one, not the edge case.
        return "customer-token", 60 * 24 * 3600
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
    async def _create_document_template(**kw):
        calls.setdefault("create_document_template", []).append(kw)
        return "TPLDOC", "pending"
    async def _fetch_template(**kw):
        calls.setdefault("fetch_template", []).append(kw)
        # Kairo's WABA holds the catalogue's *current* body — the gate compares
        # text, not just status, so the fake has to carry it. The receipt is
        # fetched by Meta's preset name verbatim, not `{locale}_{key}`.
        if kw["name"] == wt.RECEIPT_TEMPLATE_NAME:
            return meta.TemplateStatus(
                status="approved", rejection_reason=None,
                body=wt.RECEIPT_TEMPLATE_BODY,
            )
        tpl = wt.CATALOGUE.get(kw["name"].split("_", 1)[1])
        return meta.TemplateStatus(
            status="approved", rejection_reason=None,
            body=tpl.body if tpl else "",
        )
    # The two lookups that replace what the popup used to tell the browser.
    # `calls["waba_ids"]` / `calls["phone_number_ids"]` let a test make the
    # answer empty or ambiguous.
    async def _waba_ids(**kw):
        calls.setdefault("waba_lookup", []).append(kw)
        return calls.get("waba_ids", ["W-from-token"])
    async def _phone_ids(**kw):
        calls.setdefault("phone_lookup", []).append(kw)
        return calls.get("phone_number_ids", ["P-from-waba"])
    monkeypatch.setattr(meta, "waba_ids_for_token", _waba_ids)
    monkeypatch.setattr(meta, "list_phone_number_ids", _phone_ids)
    monkeypatch.setattr(meta, "exchange_code", _exchange)
    monkeypatch.setattr(meta, "subscribe_app", _subscribe)
    monkeypatch.setattr(meta, "get_phone_number", _number)
    async def _edit_template(**kw):
        calls.setdefault("edit_template", []).append(kw)
        return "pending"
    monkeypatch.setattr(meta, "create_template", _create_template)
    monkeypatch.setattr(meta, "create_document_template", _create_document_template)
    monkeypatch.setattr(meta, "edit_template", _edit_template)
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
async def test_reconnect_swaps_the_token_without_taking_the_sender_offline(monkeypatch):
    """The refresh path: an online sender legitimately redoes the popup.

    Both halves matter. `start` must not mark the row `pending_signup` — the
    salon keeps sending on the old token while the popup is open — and
    `complete` must not take the usual "already online, nothing to do" exit,
    which would leave the expiring token in place while reporting success.
    """
    sender = {"shop_id": SHOP, "source": "coexistence", "status": "online",
              "display_name": "Salone X", "phone_number": "+393331110000"}
    calls = _patch_onboarding(monkeypatch, sender=sender, calls={})

    started = await wo.start(shop_id=SHOP, display_name="Salone X",
                             settings=FakeSettings(), reconnect=True)
    assert started["signup"]["config_id"]
    assert sender["status"] == "online", "still sending while the popup is open"

    await wo.complete(shop_id=SHOP, code="c0de", waba_id="W",
                      phone_number_id="P", settings=FakeSettings(),
                      reconnect=True)

    tokens = [f["access_token"] for f in calls["fields"] if "access_token" in f]
    assert tokens == ["customer-token"], "the new token actually landed"


@pytest.mark.asyncio
async def test_reconnect_is_not_counted_against_the_new_customer_cap(monkeypatch):
    """Meta's cap counts new customers per 7 days; a refresh is not one.

    Counting it would let a busy onboarding week block an existing salon from
    renewing — its sender then dies at day 60 over someone else's signup.
    """
    calls = _patch_onboarding(
        monkeypatch,
        sender={"shop_id": SHOP, "source": "coexistence", "status": "online",
                "display_name": "Salone X", "phone_number": "+393331110000"},
        calls={"onboarded_last_7_days": 999},
    )

    result = await wo.complete(shop_id=SHOP, code="c0de", waba_id="W",
                               phone_number_id="P", settings=FakeSettings(),
                               reconnect=True)

    assert result["ok"] is True
    assert any("access_token" in f for f in calls["fields"])


@pytest.mark.asyncio
async def test_complete_records_when_the_business_token_expires(monkeypatch):
    """A 60-day token that nothing renews must leave a date behind.

    Our Login Configuration mints expiring tokens. Without this the sender
    simply stops sending on day 60 with nothing anywhere saying why — the
    token is the only credential for that WABA, and recovery is the salon
    redoing Embedded Signup, which nobody asks for if nobody knows.
    """
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={},
    )

    await wo.complete(shop_id=SHOP, code="c0de", waba_id="W", phone_number_id="P",
                      settings=FakeSettings())

    written = [f for f in calls["fields"] if "token_expires_at" in f]
    assert len(written) == 1, "expiry written exactly once, beside the token"
    expires_at = written[0]["token_expires_at"]
    delta = expires_at - datetime.now(timezone.utc)
    assert timedelta(days=59) < delta < timedelta(days=61)


@pytest.mark.asyncio
async def test_complete_leaves_expiry_null_for_a_non_expiring_token(monkeypatch):
    """Meta omits `expires_in` when the config issues non-expiring tokens.

    NULL then means "no expiry", not "unknown" — so nothing may invent one.
    """
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={},
    )

    async def _exchange(**kw):
        return "customer-token", None
    monkeypatch.setattr(meta, "exchange_code", _exchange)

    await wo.complete(shop_id=SHOP, code="c0de", waba_id="W", phone_number_id="P",
                      settings=FakeSettings())

    written = [f for f in calls["fields"] if "token_expires_at" in f]
    assert written and written[0]["token_expires_at"] is None


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
    # Pushed after the response, not inside it — one Graph round trip per
    # entry is enough to blow the gateway timeout in front of the webapp.
    assert "create_template" not in calls, \
        "a template push inside complete() is what made onboarding 504"
    await wo.ensure_templates(shop_id=SHOP, settings=FakeSettings())

    created = calls["create_template"]
    assert {c["name"] for c in created} == {
        wo.template_name(k, "it") for k in wt.CATALOGUE
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
    # Every catalogue entry failed; only the receipt (a separate create call)
    # went through.
    assert result["created"] == len(wt.DOCUMENT_TEMPLATES)
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
    assert set(result["not_ready"]) == set(wt.CATALOGUE) | set(wt.DOCUMENT_TEMPLATES)
    assert "create_template" not in calls
    assert "create_document_template" not in calls


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
    assert set(result["not_ready"]) == set(wt.CATALOGUE) | set(wt.DOCUMENT_TEMPLATES)
    assert "create_template" not in calls
    assert "create_document_template" not in calls
    assert "fetch_template" not in calls


@pytest.mark.asyncio
async def test_abort_drops_the_pending_row(monkeypatch):
    """The abandon contract: `start()` records intent, `abort()` forgets it."""
    calls = {}

    async def _delete(shop_id):
        calls["shop_id"] = shop_id
    monkeypatch.setattr(wq, "delete_pending_sender", _delete)

    result = await wo.abort(shop_id=SHOP)

    assert result == {"ok": True}
    assert calls["shop_id"] == SHOP


def test_is_abandoned_ignores_a_fresh_pending_row():
    sender = {"status": "pending_signup",
              "updated_at": datetime.now(timezone.utc) - timedelta(minutes=2)}
    assert wo.is_abandoned(sender) is False


def test_is_abandoned_flags_a_stale_pending_row():
    sender = {"status": "pending_signup",
              "updated_at": datetime.now(timezone.utc) - timedelta(minutes=20)}
    assert wo.is_abandoned(sender) is True


def test_is_abandoned_ignores_non_pending_statuses():
    sender = {"status": "online",
              "updated_at": datetime.now(timezone.utc) - timedelta(days=3)}
    assert wo.is_abandoned(sender) is False


@pytest.mark.asyncio
async def test_sweep_propagates_to_an_online_shop_once_kairo_gets_approved(monkeypatch):
    """The retry that did not exist, and its absence was silent.

    A salon onboards while Kairo's copy is still pending, so it gets nothing.
    Meta approves ours an hour later — on *Kairo's* WABA, with no per-shop
    event attached. Before this, `list_verifying_senders` didn't match (the
    salon is `online`), onboarding was over, and the panel only offered a
    manual re-push for a *rejected* template. That shop could never send and
    nothing anywhere said why.
    """
    sender = {"shop_id": SHOP, "source": "coexistence", "status": "online",
              "display_name": "Salone X", "waba_id": "WABA1", "access_token": "tok"}
    calls = _patch_onboarding(monkeypatch, sender=sender, calls={})

    async def _none():
        return []
    async def _missing(fingerprints):
        calls.setdefault("missing_query", []).append(fingerprints)
        return [sender]
    monkeypatch.setattr(wq, "list_verifying_senders", _none)
    monkeypatch.setattr(wq, "list_unresolved_templates", _none)
    monkeypatch.setattr(wq, "list_senders_needing_templates", _missing)

    counts = await wo.sweep(settings=FakeSettings())

    assert counts["propagated"] == len(wt.CATALOGUE) + len(wt.DOCUMENT_TEMPLATES)
    assert calls["missing_query"] == [wt.propagation_fingerprints()]
    assert [c["waba_id"] for c in calls["create_template"]] == ["WABA1"] * len(wt.CATALOGUE)
    # The receipt rides the same worklist, created as a document template.
    assert [c["waba_id"] for c in calls["create_document_template"]] == ["WABA1"]


@pytest.mark.asyncio
async def test_sweep_asks_kairos_waba_once_not_once_per_shop(monkeypatch):
    """Same question, same answer for everyone — N shops must not mean N calls."""
    sender = {"shop_id": SHOP, "source": "coexistence", "status": "online",
              "display_name": "Salone X", "waba_id": "WABA1", "access_token": "tok"}
    calls = _patch_onboarding(monkeypatch, sender=sender, calls={})

    async def _none():
        return []
    async def _three(fingerprints):
        return [dict(sender, shop_id=uuid4()) for _ in range(3)]
    monkeypatch.setattr(wq, "list_verifying_senders", _none)
    monkeypatch.setattr(wq, "list_unresolved_templates", _none)
    monkeypatch.setattr(wq, "list_senders_needing_templates", _three)

    await wo.sweep(settings=FakeSettings())

    # One fetch per catalogue entry per locale plus one for the receipt.
    assert len(calls["fetch_template"]) == len(wt.CATALOGUE) + len(wt.DOCUMENT_TEMPLATES)
    assert len(calls["create_template"]) == 3 * len(wt.CATALOGUE)
    assert len(calls["create_document_template"]) == 3 * len(wt.DOCUMENT_TEMPLATES)


@pytest.mark.asyncio
async def test_sweep_pushes_nothing_when_the_kairo_gate_is_empty(monkeypatch):
    """Fails closed: an unconfigured (or unreachable) gate propagates nothing."""
    sender = {"shop_id": SHOP, "source": "coexistence", "status": "online",
              "display_name": "Salone X", "waba_id": "WABA1", "access_token": "tok"}
    calls = _patch_onboarding(monkeypatch, sender=sender, calls={})

    async def _none():
        return []
    async def _boom(fingerprints):
        raise AssertionError("must not even ask for shops it cannot help")
    monkeypatch.setattr(wq, "list_verifying_senders", _none)
    monkeypatch.setattr(wq, "list_unresolved_templates", _none)
    monkeypatch.setattr(wq, "list_senders_needing_templates", _boom)

    class NoKairoWaba(FakeSettings):
        meta_kairo_waba_id = ""
        meta_kairo_token = ""

    counts = await wo.sweep(settings=NoKairoWaba())

    assert counts["propagated"] == 0
    assert "create_template" not in calls


@pytest.mark.asyncio
async def test_retire_template_deletes_kairos_copy_before_the_customers(monkeypatch):
    """Order is the design: ours first closes the gate.

    Customers-first would leave the gate still answering "approved" if the last
    step failed, and the next sweep would re-push everything just deleted.
    """
    order = []

    async def _delete(*, waba_id, name, token):
        order.append(waba_id)
    async def _senders(template_key):
        return [{"shop_id": SHOP, "waba_id": "WABA1", "access_token": "tok",
                 "name": wo.template_name(template_key, "it")}]
    dropped = []
    async def _drop(*, shop_id, template_key):
        dropped.append((shop_id, template_key))
    monkeypatch.setattr(meta, "delete_template", _delete)
    monkeypatch.setattr(wq, "list_senders_with_template", _senders)
    monkeypatch.setattr(wq, "delete_template_row", _drop)

    result = await wo.retire_template(template_key="promo_v1", settings=FakeSettings())

    assert order == ["KAIRO_WABA", "WABA1"]
    assert result["deleted"] == 1
    assert dropped == [(SHOP, "promo_v1")]


@pytest.mark.asyncio
async def test_retire_template_keeps_the_row_when_meta_refuses(monkeypatch):
    """A failed delete must stay on the worklist, not vanish from our records."""
    async def _delete(*, waba_id, name, token):
        if waba_id != "KAIRO_WABA":
            raise meta.MetaError(100, "permission denied")
    async def _senders(template_key):
        return [{"shop_id": SHOP, "waba_id": "WABA1", "access_token": "tok",
                 "name": wo.template_name(template_key, "it")}]
    async def _drop(*, shop_id, template_key):
        raise AssertionError("row dropped despite Meta still holding the template")
    monkeypatch.setattr(meta, "delete_template", _delete)
    monkeypatch.setattr(wq, "list_senders_with_template", _senders)
    monkeypatch.setattr(wq, "delete_template_row", _drop)

    result = await wo.retire_template(template_key="promo_v1", settings=FakeSettings())

    assert result["deleted"] == 0
    assert result["failed"] == [str(SHOP)]


@pytest.mark.asyncio
async def test_retire_template_refuses_an_unknown_key(monkeypatch):
    async def _boom(*a, **kw):
        raise AssertionError("must not touch Meta for a key we don't ship")
    monkeypatch.setattr(meta, "delete_template", _boom)

    result = await wo.retire_template(template_key="nope", settings=FakeSettings())
    assert result == {"ok": False, "error": "unknown_template"}


def test_push_templates_uses_the_name_the_gate_looks_for():
    """The script pushes to Kairo's WABA; the gate reads it back by name.

    They disagreed until 2026-08-31 — the script posted the bare catalogue key
    and the gate looked for the `kairo_` prefixed one, so the gate never found
    anything and no customer WABA ever received a template.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).parents[2] / "scripts" / "kairo_waba.py"
    tree = ast.parse(source.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "push_templates")
    names = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "template_name"]
    assert names, "push-templates must submit template_name(key), not the bare key"


def _load_kairo_waba():
    import importlib.util
    import pathlib

    source = pathlib.Path(__file__).parents[2] / "scripts" / "kairo_waba.py"
    spec = importlib.util.spec_from_file_location("kairo_waba", source)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_push_templates_edits_a_stale_body_and_pushes_past_the_ones_that_exist(monkeypatch):
    """The catalogue moving to v2 must actually reach Meta.

    Before 2026-09-01 this was a blind create loop over the whole catalogue and
    `_api` exited on any 4xx, so the first `name already exists` aborted the
    run: an edited body never reached Meta, and an entry added *after* the
    previous push was never submitted at all. Neither showed up in
    `kairo_waba.py templates`, which prints names and statuses, not bodies.
    """
    import argparse

    mod = _load_kairo_waba()
    monkeypatch.setenv("META_TOKEN", "t")
    monkeypatch.setenv("META_WABA_ID", "W")

    # Built from the catalogue, not hardcoded copy: this pins the behaviour,
    # not today's wording, so a template edit doesn't fail an unrelated test.
    keys = list(wt.CATALOGUE)
    stale, missing = keys[0], keys[-1]
    # The document templates are already current here — they have their own
    # test below; this one is about the catalogue.
    live = [
        {"id": f"id_{doc.name}", "name": doc.name, "language": doc.language,
         "status": "APPROVED", "category": doc.category,
         "components": [{"type": "BODY", "text": doc.body}]}
        for doc in wt.DOCUMENT_TEMPLATES.values()
    ]
    for key in keys:
        if key == missing:
            continue
        tpl = wt.CATALOGUE[key]
        body = mod._body_component(tpl)
        if key == stale:
            body = {**body, "text": "il testo che Meta ha già"}
        live.append({
            "id": f"id_{key}", "name": wo.template_name(key, tpl.language),
            "language": tpl.language,
            "status": "APPROVED", "category": tpl.category, "components": [body],
        })

    calls = []

    def fake_call(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        return 200, ({"data": live} if method == "GET" else {"status": "PENDING"})

    monkeypatch.setattr(mod, "_call", fake_call)
    mod.push_templates(argparse.Namespace(dry_run=False))

    posts = [(path, payload) for method, path, payload in calls if method == "POST"]
    edited = [p for p in posts if p[0] == f"id_{stale}"]
    created = [p for p in posts if p[1] and p[1].get("name") == wo.template_name(missing, "it")]
    assert edited, "a changed body must be edited on the template it belongs to"
    assert edited[0][1]["components"][0]["text"] == wt.CATALOGUE[stale].body
    assert created, "an entry added after the last push must still be created"
    # Everything else was already identical: re-submitting burns Meta's edit
    # quota and puts an approved template back into review for nothing.
    assert len(posts) == 2, posts


def test_push_templates_creates_the_document_template_with_its_sample(monkeypatch):
    """The receipt pushes from the repo like everything else.

    It is a DOCUMENT header, so it needs a HEADER component and a publicly
    hosted sample for Meta to review — the reason it can't ride the catalogue
    loop, and the reason it used to be built by hand in WhatsApp Manager.
    """
    import argparse

    mod = _load_kairo_waba()
    monkeypatch.setenv("META_TOKEN", "t")
    monkeypatch.setenv("META_WABA_ID", "W")
    monkeypatch.setenv("META_RECEIPT_SAMPLE_URL", "https://example.test/sample.pdf")

    # Every catalogue entry is already current; only the document is missing.
    live = [
        {"id": f"id_{key}", "name": wo.template_name(key, tpl.language),
         "language": tpl.language, "status": "APPROVED", "category": tpl.category,
         "components": [mod._body_component(tpl)]}
        for key, tpl in wt.CATALOGUE.items()
    ]
    calls = []

    def fake_call(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        return 200, ({"data": live} if method == "GET" else {"status": "PENDING"})

    monkeypatch.setattr(mod, "_call", fake_call)
    mod.push_templates(argparse.Namespace(dry_run=False))

    posts = [payload for method, _, payload in calls if method == "POST"]
    assert len(posts) == 1, posts
    doc = wt.DOCUMENT_TEMPLATES["purchase_receipt_1"]
    assert posts[0]["name"] == doc.name, "the preset name is used verbatim, not it_-prefixed"
    header = posts[0]["components"][0]
    assert header["format"] == "DOCUMENT"
    assert header["example"]["header_handle"] == ["https://example.test/sample.pdf"]


def test_push_templates_refuses_to_create_a_document_without_a_sample(monkeypatch):
    """No sample URL means Meta rejects it — say so instead of submitting."""
    import argparse

    mod = _load_kairo_waba()
    monkeypatch.setenv("META_TOKEN", "t")
    monkeypatch.setenv("META_WABA_ID", "W")
    monkeypatch.delenv("META_RECEIPT_SAMPLE_URL", raising=False)

    live = [
        {"id": f"id_{key}", "name": wo.template_name(key, tpl.language),
         "language": tpl.language, "status": "APPROVED", "category": tpl.category,
         "components": [mod._body_component(tpl)]}
        for key, tpl in wt.CATALOGUE.items()
    ]
    posts = []

    def fake_call(method, path, **kwargs):
        if method == "POST":
            posts.append(path)
        return 200, ({"data": live} if method == "GET" else {"status": "PENDING"})

    monkeypatch.setattr(mod, "_call", fake_call)
    with pytest.raises(SystemExit):
        mod.push_templates(argparse.Namespace(dry_run=False))
    assert posts == []


# ------------------------------------------------------- copy drift downstream

@pytest.mark.asyncio
async def test_ensure_templates_edits_a_template_whose_body_changed(monkeypatch):
    """Re-voicing a template must reach the salons already carrying it.

    Before `body_hash`, an existing row was an unconditional skip: every
    connected salon kept sending the old text forever while `status` read
    `approved` and nothing anywhere disagreed.
    """
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )
    key = next(iter(wt.CATALOGUE))

    async def _get_template(shop_id, template_key):
        if template_key != key:
            return None
        return {"template_key": key, "meta_template_id": f"id_{key}",
                "status": "approved", "body_hash": "the-old-copy"}
    monkeypatch.setattr(wq, "get_template", _get_template)

    async def _edit(**kw):
        calls.setdefault("edit_template", []).append(kw)
        return "pending"
    monkeypatch.setattr(meta, "edit_template", _edit)

    result = await wo.ensure_templates(shop_id=SHOP, settings=FakeSettings())

    assert result["edited"] == 1
    # The other catalogue keys are created, plus the receipt (its own create).
    assert result["created"] == len(wt.CATALOGUE) - 1 + len(wt.DOCUMENT_TEMPLATES)
    edit = calls["edit_template"][0]
    # Edited in place, on the template it belongs to: delete-and-recreate would
    # take the salon off the air for Meta's 30-day name lock.
    assert edit["template_id"] == f"id_{key}"
    assert edit["body_text"] == wt.CATALOGUE[key].body
    assert "create_template" not in [c.get("name") for c in calls.get("create_template", [])
                                     if c.get("name") == wo.template_name(key, "it")]
    row = next(t for t in calls["templates"] if t["template_key"] == key)
    assert row["body_hash"] == wt.body_hash(wt.CATALOGUE[key].body)
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_ensure_templates_leaves_a_current_body_alone(monkeypatch):
    """An unchanged body must not be resubmitted: it would burn Meta's edit
    quota and put an approved template back into review for nothing."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )

    async def _get_template(shop_id, template_key):
        tpl = (wt.CATALOGUE.get(template_key)
               or wt.DOCUMENT_TEMPLATES.get(template_key))
        return {"template_key": template_key, "meta_template_id": f"id_{template_key}",
                "status": "approved", "body_hash": wt.body_hash(tpl.body)}
    monkeypatch.setattr(wq, "get_template", _get_template)

    async def _edit(**kw):
        raise AssertionError("nothing changed — nothing must be pushed")
    monkeypatch.setattr(meta, "edit_template", _edit)

    result = await wo.ensure_templates(shop_id=SHOP, settings=FakeSettings())

    assert result == {"ok": True, "created": 0, "edited": 0,
                      "failed": [], "not_ready": []}
    assert "create_template" not in calls
    assert "create_document_template" not in calls


@pytest.mark.asyncio
async def test_ensure_templates_does_not_push_new_copy_kairo_has_not_approved(monkeypatch):
    """The gate compares the *body*, not just the status of a name.

    Change the copy here and deploy before `push-templates` runs: Kairo's WABA
    still reports APPROVED — for last month's text. A name-only gate would read
    that as a green light and push the unreviewed copy to every customer WABA.
    """
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )

    async def _stale(**kw):
        return meta.TemplateStatus(status="approved", rejection_reason=None,
                                   body="il testo che Meta ha già")
    monkeypatch.setattr(meta, "fetch_template", _stale)

    async def _get_template(shop_id, template_key):
        return {"template_key": template_key, "meta_template_id": "id_x",
                "status": "approved", "body_hash": "the-old-copy"}
    monkeypatch.setattr(wq, "get_template", _get_template)

    async def _edit(**kw):
        raise AssertionError("Kairo's WABA has not approved this copy")
    monkeypatch.setattr(meta, "edit_template", _edit)

    result = await wo.ensure_templates(shop_id=SHOP, settings=FakeSettings())

    assert result["edited"] == 0
    assert set(result["not_ready"]) == set(wt.CATALOGUE) | set(wt.DOCUMENT_TEMPLATES)
    del calls


def test_propagation_fingerprints_cover_the_receipt_the_worklist_must_visit():
    """The sweep's worklist key: one entry per pushed template, hash of its body.

    Catalogue + document templates are the push list. Before 2026-09-23 the
    worklist was catalogue-only, and the rationale ran the other way: a
    `purchase_receipt_1` row was treated as *padding* that could make a shop
    look complete while it missed a real template. With the receipt now
    propagated proactively by the sweep, the reversal is the point — a shop
    holding every catalogue entry but no receipt row must come back, and a
    shop whose receipt hash matches must not. (The padding bug is still dead:
    the count is matched against `key|hash` pairs, never rows.)
    """
    catalogue = wt.catalogue_fingerprints()
    assert len(catalogue) == len(wt.CATALOGUE)
    assert all("|" in f for f in catalogue)
    key, tpl = next(iter(wt.CATALOGUE.items()))
    assert f"{key}|{wt.body_hash(tpl.body)}" in catalogue
    assert wt.body_hash(tpl.body) != wt.body_hash(tpl.body + " ")

    doc = wt.DOCUMENT_TEMPLATES[wt.RECEIPT_TEMPLATE_KEY]
    assert wt.document_fingerprints() == [
        f"{wt.RECEIPT_TEMPLATE_KEY}|{wt.body_hash(doc.body)}"
    ]
    # The receipt is one of the pushed, so it is one of the counted: a shop
    # missing it shows up on the worklist, a shop holding the current copy
    # does not.
    assert wt.propagation_fingerprints() == catalogue + wt.document_fingerprints()
    assert f"{wt.RECEIPT_TEMPLATE_KEY}|{wt.body_hash(doc.body)}" in wt.propagation_fingerprints()


def test_signup_config_asks_meta_for_the_coexistence_branch():
    """Without this flag the popup offers only a brand-new WABA.

    The salon would be told to delete their WhatsApp Business App — exactly
    the Twilio behaviour this migration exists to avoid.
    """
    config = wo.signup_config(FakeSettings())
    assert config["feature_type"] == "whatsapp_business_app_onboarding"
    assert config["config_id"] == "cfg"
    assert config["solution_id"] == "sol"


def test_template_names_compose_the_locale_with_the_key():
    """Meta scopes names per-WABA and cannot translate a template.

    So the platform addresses a locale's copy by composing the name, never by
    looking one up: `it_promo_v1` today, `en_promo_v1` the day English copy
    exists on the same WABA. Still one name per (locale, key) across all shops.
    """
    assert wo.template_name("promo_v1", "it") == "it_promo_v1"
    assert wo.template_name("promo_v1", "en") == "en_promo_v1"


def test_a_shop_on_an_unsupported_locale_falls_back_to_copy_that_exists():
    """Spanish is in the platform's locales, not (yet) in the catalogue.

    Composing `es_promo_v1` for that shop would name a template on no WABA:
    every template reads `missing` and nothing says why. Italian is wrong copy
    but a real, approved template.
    """
    assert wt.resolve_language("it") == "it"
    assert wt.resolve_language("es") == "it"
    assert wt.resolve_language(None) == "it"


@pytest.mark.asyncio
async def test_ensure_templates_names_the_shops_own_locale(monkeypatch):
    """The salon's `shops.language` is what decides which copy it receives."""
    calls = _patch_onboarding(
        monkeypatch,
        sender={"shop_id": SHOP, "waba_id": "WABA1", "access_token": "tok"},
        calls={"language": "it"},
    )

    await wo.ensure_templates(shop_id=SHOP, settings=FakeSettings())

    created = calls["create_template"]
    assert {c["name"] for c in created} == {
        f"it_{key}" for key in wt.CATALOGUE
    }
    assert {c["language"] for c in created} == {"it"}
    # The receipt is fetched and created by its verbatim preset name, never
    # locale-prefixed.
    assert calls["create_document_template"][0]["name"] == wt.RECEIPT_TEMPLATE_NAME
    assert calls["create_document_template"][0]["language"] == wt.RECEIPT_TEMPLATE_LANGUAGE
    # The gate was asked about the same locale it then created, or a shop can
    # be told a template is ready and be given one that is not.
    assert {f["name"] for f in calls["fetch_template"]} == {
        f"it_{key}" for key in wt.CATALOGUE
    } | {wt.RECEIPT_TEMPLATE_NAME}


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
        # A row, not None: None means "Meta replayed this", and the route
        # would then skip the worker the assertions below are meant to cover.
        return {"id": uuid4(), **kw}
    monkeypatch.setattr(wa_routes.wq, "record_inbound", _fake_record_inbound)
    scheduled = []
    monkeypatch.setattr(wa_routes.wa_inbound, "schedule",
                        lambda sender, row: scheduled.append(row))

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
    # The reply is also handed to the inbound worker, which names it.
    assert len(scheduled) == 1


def test_template_descriptor_carries_what_the_generator_needs():
    """The webapp picker and the engine prompt both read this one payload.
    A descriptor missing `body` or `generated_slot` produces a prompt with no
    frame — the model then writes a standalone sentence that reads as a non
    sequitur inside the approved scaffolding."""
    from booking_engine.api.routes.whatsapp import _template_descriptor

    d = _template_descriptor("winback_v1")
    assert d["template_key"] == "winback_v1"
    assert d["category"] == "MARKETING"
    assert d["generated_slot"] == 5
    assert "{{5}}" in d["body"]
    assert d["max_chars"] == 90
    assert d["intent"] == "winback"
    assert d["guidance"]
    assert d["language"] == "it"


def test_descriptor_keeps_the_field_name_its_consumers_read():
    """`template_key` is the name the DB column, CampaignRequest and three
    webapp components all use. Renaming it here does not fail any test in this
    repo — it fails silently in the webapp, where BulkCampaignTile gates sending
    on `t.template_key === 'promo_v1'` and would simply stop sending."""
    from booking_engine.api.routes.whatsapp import _template_descriptor

    for key in wt.CATALOGUE:
        d = _template_descriptor(key)
        assert "template_key" in d, f"{key}: consumers read template_key"
        assert "key" not in d, f"{key}: two names for one field will drift"


def test_utility_descriptor_reports_no_generated_slot():
    from booking_engine.api.routes.whatsapp import _template_descriptor

    assert _template_descriptor("reminder_v6")["generated_slot"] is None


def test_the_feedback_descriptor_is_marketing_with_no_generated_slot():
    """feedback_v2 is the category split's one exception: MARKETING, but every
    variable is still a fact, so the webapp must be told there is no slot and
    not offer a generate button for it."""
    from booking_engine.api.routes.whatsapp import _template_descriptor

    d = _template_descriptor("feedback_v2")
    assert d["category"] == "MARKETING"
    assert d["generated_slot"] is None
    assert d["filled_by"] is None


def test_descriptor_reports_who_fills_the_slot():
    """The webapp picks its UI from this: an LLM-filled template gets a
    generate button, an owner-filled one gets a textarea."""
    from booking_engine.api.routes.whatsapp import _template_descriptor

    assert _template_descriptor("promo_v1")["filled_by"] == "llm"
    assert _template_descriptor("promo_manual_v1")["filled_by"] == "owner"
    assert _template_descriptor("reminder_v6")["filled_by"] is None


@pytest.mark.asyncio
async def test_complete_reads_the_ids_back_from_the_token(monkeypatch):
    """The browser has only the code, so the ids come from Meta, not the popup.

    `WA_EMBEDDED_SIGNUP` — the message carrying waba_id and phone_number_id —
    is only posted when the flow runs through Meta's JS SDK, and ours cannot
    (the SDK routes FB.login through FedCM and drops `config_id`). Observed on
    a real signup: the code arrived, that message never did.
    """
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={},
    )

    result = await wo.complete(shop_id=SHOP, code="c0de", settings=FakeSettings())

    assert result["ok"] is True
    written = [f for f in calls["fields"] if "waba_id" in f]
    assert written[0]["waba_id"] == "W-from-token"
    assert written[0]["phone_number_id"] == "P-from-waba"
    # The phone lookup must ask the WABA we just derived, not some default.
    assert calls["phone_lookup"][0]["waba_id"] == "W-from-token"


@pytest.mark.asyncio
async def test_complete_refuses_an_ambiguous_waba_instead_of_guessing(monkeypatch):
    """Two granted WABAs cannot be resolved by picking one.

    Guessing wrong attaches the salon's sender to someone else's WhatsApp
    account — unrecoverable without noticing, and nothing downstream would
    disagree. A named error is the only honest outcome.
    """
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={"waba_ids": ["W1", "W2"]},
    )

    result = await wo.complete(shop_id=SHOP, code="c0de", settings=FakeSettings())

    assert result["ok"] is False
    assert result["error"] == "waba_ambiguous"
    assert not any("waba_id" in f for f in calls.get("fields", [])), \
        "no sender written on an unresolved WABA"


@pytest.mark.asyncio
async def test_complete_still_accepts_ids_supplied_by_the_caller(monkeypatch):
    """An SDK-based caller that does have them keeps working, and skips the lookup."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={},
    )

    result = await wo.complete(shop_id=SHOP, code="c0de", waba_id="W-explicit",
                               phone_number_id="P-explicit", settings=FakeSettings())

    assert result["ok"] is True
    written = [f for f in calls["fields"] if "waba_id" in f]
    assert written[0]["waba_id"] == "W-explicit"
    assert "waba_lookup" not in calls, "no lookup when the caller already knows"


@pytest.mark.asyncio
@respx.mock
async def test_exchange_code_repeats_the_dialog_redirect_uri():
    """Meta binds the code to the redirect_uri; omitting it fails the exchange.

    This is not defensive: the hand-built OAuth dialog always opens with one,
    so every real signup goes through this branch.
    """
    route = respx.get(f"{meta.GRAPH}/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 100}),
    )

    token, expires_in = await meta.exchange_code(
        code="c0de", app_id="A", app_secret="S",
        redirect_uri="https://qa.example.test/",
    )

    assert (token, expires_in) == ("t", 100)
    assert route.calls.last.request.url.params["redirect_uri"] == "https://qa.example.test/"


@pytest.mark.asyncio
@respx.mock
async def test_exchange_code_omits_redirect_uri_when_there_is_none():
    """Meta's own SDK flow has no redirect, and sending an empty one is refused."""
    route = respx.get(f"{meta.GRAPH}/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "t"}),
    )

    await meta.exchange_code(code="c0de", app_id="A", app_secret="S")

    assert "redirect_uri" not in route.calls.last.request.url.params


@pytest.mark.asyncio
async def test_ambiguous_waba_keeps_the_token_and_names_the_candidates(monkeypatch):
    """The question has to be answerable: the code is spent once it is asked."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X"},
        calls={"waba_ids": ["W1", "W2"]},
    )
    async def _name(*, waba_id, token):
        return {"W1": "Salone X", "W2": "Altra azienda"}[waba_id]
    async def _numbers(*, waba_id, token):
        return {"W1": ["+39 02 1234567"], "W2": []}[waba_id]
    monkeypatch.setattr(meta, "get_waba_name", _name)
    monkeypatch.setattr(meta, "list_phone_numbers", _numbers)

    result = await wo.complete(shop_id=SHOP, code="c0de", settings=FakeSettings())

    assert result["error"] == "waba_ambiguous"
    # The number is what the owner recognises; the name is often a company
    # registration they have never read.
    assert result["wabas"] == [
        {"id": "W1", "name": "Salone X", "phone_numbers": ["+39 02 1234567"]},
        {"id": "W2", "name": "Altra azienda", "phone_numbers": []},
    ]
    written = [f for f in calls["fields"] if "access_token" in f]
    assert written and written[0]["access_token"] == "customer-token", \
        "token persisted before the question, or the answer needs a second popup"


@pytest.mark.asyncio
async def test_naming_the_waba_resumes_without_re_exchanging_the_code(monkeypatch):
    """Second call, no code: it reads the token back off the row."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X",
                             "access_token": "customer-token"},
        calls={},
    )

    result = await wo.complete(shop_id=SHOP, waba_id="W1", settings=FakeSettings())

    assert result["ok"] is True and result["status"] == "online"
    assert not calls.get("exchange"), "a spent code must not be exchanged again"
    assert calls["subscribe"][0]["token"] == "customer-token"


@pytest.mark.asyncio
async def test_resume_does_not_clear_the_recorded_expiry(monkeypatch):
    """The second call knows no expires_in; it must not overwrite the date."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "pending_signup", "display_name": "Salone X",
                             "access_token": "customer-token"},
        calls={},
    )

    await wo.complete(shop_id=SHOP, waba_id="W1", settings=FakeSettings())

    assert not any("token_expires_at" in f for f in calls["fields"]), \
        "resuming would silently make an expiring token look non-expiring"


@pytest.mark.asyncio
async def test_a_template_meta_already_holds_is_adopted_not_refused(monkeypatch):
    """Meta re-categorises on review, which refuses every later create.

    The first real onboarding (2026-09-20) created all six on the WABA, then
    every re-push failed forever — Meta had moved a UTILITY body to MARKETING,
    and we kept resubmitting our own category. Nothing was recorded, so the
    sweep retried the identical create hourly while the panel said the feature
    was waiting for Meta.
    """
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "customer-token"},
        calls={},
    )
    async def _refuse(**kw):
        raise meta.MetaError(100, "The category UTILITY doesn't match", 2388026)
    async def _found(*, waba_id, name, token):
        return meta.TemplateStatus(
            status="pending", rejection_reason=None,
            body="corpo che Meta tiene", id="META-1", category="MARKETING",
        )
    monkeypatch.setattr(meta, "create_template", _refuse)
    monkeypatch.setattr(meta, "fetch_template", _found)

    # The gate is answered here so the fake `fetch_template` above only ever
    # serves adoption — `approved_on_kairo_waba` uses the same call.
    result = await wo.ensure_templates(
        shop_id=SHOP, settings=FakeSettings(),
        approved={("it", k) for k in wt.CATALOGUE},
    )

    assert result["failed"] == [], "an existing template is not a failure"
    written = calls["templates"]
    assert written, "adopting must record the row, or the sweep retries forever"
    row = written[0]
    assert row["meta_template_id"] == "META-1"
    # Meta's category, not ours: storing our guess is what makes the next
    # create repeat the same refusal.
    assert row["category"] == "MARKETING"
    # Meta's body, so stale copy reads as drift and the edit path fixes it.
    assert row["body_hash"] == wo.body_hash("corpo che Meta tiene")


@pytest.mark.asyncio
async def test_a_template_meta_does_not_have_is_still_a_failure(monkeypatch):
    """Adoption must not turn a genuine rejection into a silent success."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "customer-token"},
        calls={},
    )
    async def _refuse(**kw):
        raise meta.MetaError(100, "Invalid parameter", None)
    async def _absent(**kw):
        return None
    monkeypatch.setattr(meta, "create_template", _refuse)
    monkeypatch.setattr(meta, "fetch_template", _absent)

    result = await wo.ensure_templates(
        shop_id=SHOP, settings=FakeSettings(),
        approved={("it", k) for k in wt.CATALOGUE},
    )

    assert set(result["failed"]) == set(wt.CATALOGUE)
    assert not calls.get("templates")


# ---------------------------------------------------- receipt rides the sweep

def _patch_kairo_gate(monkeypatch, *, receipt_verdict):
    """The gate's fetch fake: catalogue bodies from the catalogue, the receipt
    by its verbatim preset name with the verdict the test chooses."""
    calls = []
    async def _fetch(*, waba_id, name, token):
        calls.append({"waba_id": waba_id, "name": name, "token": token})
        if name == wt.RECEIPT_TEMPLATE_NAME:
            return receipt_verdict
        tpl = wt.CATALOGUE.get(name.split("_", 1)[1])
        return meta.TemplateStatus(status="approved", rejection_reason=None,
                                   body=tpl.body if tpl else "")
    monkeypatch.setattr(meta, "fetch_template", _fetch)
    return calls


@pytest.mark.asyncio
async def test_approved_on_kairo_waba_includes_the_receipt_once_kairo_holds_it(monkeypatch):
    """The receipt is in the gate's answer, fetched by its verbatim name.

    That pair is what unblocks the sweep's document loop, exactly like a
    catalogue pair unblocks `create_template`.
    """
    calls = _patch_kairo_gate(monkeypatch, receipt_verdict=meta.TemplateStatus(
        status="approved", rejection_reason=None, body=wt.RECEIPT_TEMPLATE_BODY))

    approved = await wo.approved_on_kairo_waba(FakeSettings())

    assert approved == ({("it", k) for k in wt.CATALOGUE}
                        | {(wt.RECEIPT_TEMPLATE_LANGUAGE, wt.RECEIPT_TEMPLATE_KEY)})
    assert wt.RECEIPT_TEMPLATE_NAME in [c["name"] for c in calls]
    assert f"it_{wt.RECEIPT_TEMPLATE_KEY}" not in [c["name"] for c in calls]


@pytest.mark.asyncio
async def test_approved_on_kairo_waba_excludes_a_receipt_whose_body_drifted(monkeypatch):
    """Same rule as the catalogue: approved *name* with different copy is not
    approved — pushing it would hand unreviewed text to every salon."""
    _patch_kairo_gate(monkeypatch, receipt_verdict=meta.TemplateStatus(
        status="approved", rejection_reason=None, body="il testo che Meta ha già"))

    approved = await wo.approved_on_kairo_waba(FakeSettings())

    assert approved == {("it", k) for k in wt.CATALOGUE}


@pytest.mark.asyncio
async def test_approved_on_kairo_waba_excludes_a_receipt_not_yet_ruled_on(monkeypatch):
    """Pending is not approved — the gate fails closed on a missing verdict."""
    _patch_kairo_gate(monkeypatch, receipt_verdict=meta.TemplateStatus(
        status="pending", rejection_reason=None, body=wt.RECEIPT_TEMPLATE_BODY))

    approved = await wo.approved_on_kairo_waba(FakeSettings())

    assert (wt.RECEIPT_TEMPLATE_LANGUAGE, wt.RECEIPT_TEMPLATE_KEY) not in approved


@pytest.mark.asyncio
async def test_approved_on_kairo_waba_fails_closed_when_unconfigured():
    class NoKairoWaba(FakeSettings):
        meta_kairo_waba_id = ""
        meta_kairo_token = ""

    assert await wo.approved_on_kairo_waba(NoKairoWaba()) == set()


def _current_rows_or(monkeypatch, receipt_row):
    """get_template: current-hash rows for the catalogue, `receipt_row` for the
    receipt — so catalogue noise stays out of the receipt-loop assertions."""
    async def _get_template(shop_id, template_key):
        if template_key == wt.RECEIPT_TEMPLATE_KEY:
            return receipt_row
        tpl = wt.CATALOGUE[template_key]
        return {"template_key": template_key,
                "meta_template_id": f"id_{template_key}",
                "status": "approved", "body_hash": wt.body_hash(tpl.body)}
    monkeypatch.setattr(wq, "get_template", _get_template)


@pytest.mark.asyncio
async def test_ensure_templates_creates_the_receipt_as_a_document_template(monkeypatch):
    """Proactive propagation: same gate, a document create, a verbatim name."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )
    _current_rows_or(monkeypatch, receipt_row=None)
    approved = ({("it", k) for k in wt.CATALOGUE}
                | {(wt.RECEIPT_TEMPLATE_LANGUAGE, wt.RECEIPT_TEMPLATE_KEY)})

    result = await wo.ensure_templates(
        shop_id=SHOP, settings=FakeSettings(), approved=approved,
    )

    created = calls["create_document_template"]
    assert len(created) == len(wt.DOCUMENT_TEMPLATES)
    doc = created[0]
    assert doc["waba_id"] == "WABA1" and doc["token"] == "tok"
    assert doc["name"] == wt.RECEIPT_TEMPLATE_NAME
    assert doc["language"] == wt.RECEIPT_TEMPLATE_LANGUAGE
    assert doc["category"] == "UTILITY"
    assert doc["body_text"] == wt.RECEIPT_TEMPLATE_BODY
    assert doc["example_url"] == FakeSettings.meta_receipt_sample_url
    row = next(t for t in calls["templates"]
               if t["template_key"] == wt.RECEIPT_TEMPLATE_KEY)
    assert row["name"] == wt.RECEIPT_TEMPLATE_NAME
    assert row["variable_count"] == 0
    assert row["body_hash"] == wt.body_hash(wt.RECEIPT_TEMPLATE_BODY)
    # The catalogue rows are already current in this fixture: only the receipt
    # is pushed, and it lands through the document create, not `create_template`.
    assert result["created"] == len(wt.DOCUMENT_TEMPLATES)
    assert "create_template" not in calls
    assert wt.RECEIPT_TEMPLATE_KEY not in result["failed"]
    assert wt.RECEIPT_TEMPLATE_KEY not in result["not_ready"]


@pytest.mark.asyncio
async def test_ensure_templates_edits_a_stale_receipt_body_only(monkeypatch):
    """An approved receipt whose hash no longer matches is edited in place —
    body only, never the header (Meta never hands the handle back, so a
    header resubmit would need the sample URL again for no gain)."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )
    _current_rows_or(monkeypatch, receipt_row={
        "template_key": wt.RECEIPT_TEMPLATE_KEY,
        "meta_template_id": "RECEIPT-ID", "status": "approved",
        "body_hash": "il-corpo-che-meta-ha"})
    approved = ({("it", k) for k in wt.CATALOGUE}
                | {(wt.RECEIPT_TEMPLATE_LANGUAGE, wt.RECEIPT_TEMPLATE_KEY)})

    result = await wo.ensure_templates(
        shop_id=SHOP, settings=FakeSettings(), approved=approved,
    )

    assert "create_document_template" not in calls
    # The catalogue rows are already current here: the one edit is the receipt.
    edits = calls["edit_template"]
    assert len(edits) == len(wt.DOCUMENT_TEMPLATES)
    receipt_edit = edits[0]
    assert receipt_edit["template_id"] == "RECEIPT-ID"
    assert receipt_edit["body_text"] == wt.RECEIPT_TEMPLATE_BODY
    assert receipt_edit["sample_variables"] == {}
    assert result["edited"] == len(wt.DOCUMENT_TEMPLATES)


@pytest.mark.asyncio
async def test_ensure_templates_leaves_a_receipt_meta_is_still_reviewing_alone(monkeypatch):
    """Meta refuses to edit a template under review — the same skip as the
    catalogue, and the row is revisited on the first sweep after the verdict."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )
    _current_rows_or(monkeypatch, receipt_row={
        "template_key": wt.RECEIPT_TEMPLATE_KEY,
        "meta_template_id": "RECEIPT-ID", "status": "pending",
        "body_hash": "il-corpo-che-meta-ha"})
    approved = ({("it", k) for k in wt.CATALOGUE}
                | {(wt.RECEIPT_TEMPLATE_LANGUAGE, wt.RECEIPT_TEMPLATE_KEY)})

    result = await wo.ensure_templates(
        shop_id=SHOP, settings=FakeSettings(), approved=approved,
    )

    assert result["created"] == 0 and result["edited"] == 0
    assert not calls.get("edit_template")
    assert "create_document_template" not in calls


@pytest.mark.asyncio
async def test_ensure_templates_reports_the_receipt_not_ready_when_the_gate_has_it_pending(monkeypatch):
    """Kairo's copy not approved means the receipt waits with the catalogue."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )
    _current_rows_or(monkeypatch, receipt_row=None)

    result = await wo.ensure_templates(
        shop_id=SHOP, settings=FakeSettings(),
        approved={("it", k) for k in wt.CATALOGUE},
    )

    assert result["created"] == 0
    assert wt.RECEIPT_TEMPLATE_KEY in result["not_ready"]
    assert "create_document_template" not in calls


@pytest.mark.asyncio
async def test_ensure_templates_fails_soft_on_the_receipt_without_a_sample_url(monkeypatch):
    """A missing `META_RECEIPT_SAMPLE_URL` is reported as not_ready, never a
    guaranteed-rejection create — and the catalogue loop above is untouched."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={},
    )
    _current_rows_or(monkeypatch, receipt_row=None)

    class NoSample(FakeSettings):
        meta_receipt_sample_url = ""

    result = await wo.ensure_templates(
        shop_id=SHOP, settings=NoSample(),
        approved={("it", k) for k in wt.CATALOGUE}
        | {(wt.RECEIPT_TEMPLATE_LANGUAGE, wt.RECEIPT_TEMPLATE_KEY)},
    )

    assert result["ok"] is True
    assert wt.RECEIPT_TEMPLATE_KEY in result["not_ready"]
    assert "create_document_template" not in calls


def test_worklist_counts_fingerprint_pairs_not_rows():
    """A shop with every catalogue fingerprint but no receipt row must appear;
    one whose receipt hash matches must not. The query knows nothing about
    which templates exist — appearance is decided entirely by the fingerprint
    array the sweep passes it (`propagation_fingerprints`, pinned above), so
    what is pinned here is the array-driven shape of the SQL."""
    import inspect
    source = inspect.getsource(wq.list_senders_needing_templates)
    assert "t.template_key || '|' || coalesce(t.body_hash, '')" in source
    assert "= ANY($1)" in source
    assert "cardinality($1::text[])" in source


# ---------------------------------------------------------------- secret box

def test_sealed_token_round_trips_and_is_not_readable_at_rest():
    """The one credential with no parent behind it must not sit in plaintext."""
    from cryptography.fernet import Fernet
    from booking_engine.services import secret_box as sb

    key = Fernet.generate_key().decode()
    stored = sb.seal("EAAG-real-business-token", key)

    assert "EAAG-real-business-token" not in stored, "a dump would read it"
    assert stored.startswith("v1:"), "the prefix is what tells sealed from legacy"
    assert sb.unseal(stored, key) == "EAAG-real-business-token"


def test_legacy_plaintext_reads_without_a_migration():
    """Rows written before the key existed still open — that is the rollout."""
    from cryptography.fernet import Fernet
    from booking_engine.services import secret_box as sb

    assert sb.unseal("EAAG-legacy", Fernet.generate_key().decode()) == "EAAG-legacy"
    assert sb.unseal("EAAG-legacy", "") == "EAAG-legacy"


def test_unconfigured_stores_plaintext_rather_than_taking_whatsapp_offline():
    from booking_engine.services import secret_box as sb
    assert sb.seal("EAAG-x", "") == "EAAG-x"


def test_a_sealed_token_never_silently_becomes_its_own_ciphertext():
    """Returning ciphertext would reach Meta as a bearer token and come back as
    a generic auth error — read as an expired token, sending the salon through
    a reconnect that cannot fix it."""
    from cryptography.fernet import Fernet
    from booking_engine.services import secret_box as sb

    stored = sb.seal("EAAG-x", Fernet.generate_key().decode())
    with pytest.raises(sb.SecretBoxError):
        sb.unseal(stored, "")
    with pytest.raises(sb.SecretBoxError):
        sb.unseal(stored, Fernet.generate_key().decode())


@pytest.mark.asyncio
async def test_a_verdict_for_a_template_we_never_recorded_is_reported(monkeypatch):
    """Silent until 2026-09-20, when six templates did exactly this.

    Meta ruling on a name we hold no row for updates nothing. Reporting the
    miss is what makes the gap audible while the sweep's adoption repairs it.
    """
    seen = {}
    async def _set(**kw):
        seen.update(kw)
        return False  # no row matched
    monkeypatch.setattr(wq, "set_template_status", _set)
    warnings = []
    monkeypatch.setattr(
        whatsapp_routes.logger, "warning",
        lambda msg, *a: warnings.append(msg % a if a else msg),
    )

    await whatsapp_routes._handle_change(
        {"shop_id": SHOP},
        {"field": "message_template_status_update",
         "value": {"message_template_name": "it_promo_v1", "event": "APPROVED"}},
    )

    assert seen["name"] == "it_promo_v1"
    assert any("template_verdict_unmatched" in w for w in warnings)


async def _noop():
    return None


async def _run_sweep_stage(monkeypatch):
    """Run sweep() with every stage but the reminder stubbed to nothing."""
    async def _none(*a, **kw):
        return []
    async def _empty_set(*a, **kw):
        return set()
    for name in ("list_verifying_senders", "list_senders_needing_templates",
                 "list_unresolved_templates"):
        monkeypatch.setattr(wq, name, _none)
    monkeypatch.setattr(wo, "approved_on_kairo_waba", _empty_set)
    return await wo.sweep(settings=FakeSettings())


@pytest.mark.asyncio
async def test_the_tick_emails_once_per_window_not_once_per_hour(monkeypatch):
    """The banner is pull-only; email is the only warning that reaches a salon
    whose owner does not open the app in the week that matters."""
    sent, marked = [], []
    async def _due(*, window_days, cooldown_hours):
        sent.append((window_days, cooldown_hours))
        return [{"shop_id": SHOP, "phone_number": "+39 02 1", "days_left": 3}]
    async def _notify(**kw):
        sent.append(kw)
        return True
    async def _mark(shop_id):
        marked.append(shop_id)
    monkeypatch.setattr(wq, "list_senders_needing_token_reminder", _due)
    monkeypatch.setattr(wq, "mark_token_reminder_sent", _mark)
    monkeypatch.setattr(wo.webapp_notify, "whatsapp_token_expiring", _notify)

    await _run_sweep_stage(monkeypatch)

    assert sent[0] == (wo.RENEW_WINDOW_DAYS, wo.REMINDER_COOLDOWN_HOURS)
    assert sent[1]["days_left"] == 3
    assert marked == [SHOP], "unmarked means the next tick mails again in an hour"


@pytest.mark.asyncio
async def test_a_salon_with_no_mailbox_is_not_retried_every_hour(monkeypatch):
    """The attempt is recorded, not the delivery — an hourly retry against a
    shop that simply has no owner email is noise, and the banner still covers
    that salon."""
    marked = []
    async def _due(**kw):
        return [{"shop_id": SHOP, "phone_number": None, "days_left": 0}]
    async def _notify(**kw):
        return False  # no owner email, or Resend refused
    monkeypatch.setattr(wq, "list_senders_needing_token_reminder", _due)
    monkeypatch.setattr(wq, "mark_token_reminder_sent",
                        lambda shop_id: marked.append(shop_id) or _noop())
    monkeypatch.setattr(wo.webapp_notify, "whatsapp_token_expiring", _notify)

    await _run_sweep_stage(monkeypatch)

    assert marked == [SHOP]


@pytest.mark.asyncio
async def test_a_template_under_review_is_left_alone_however_drifted(monkeypatch):
    """Editing one Meta is still reviewing either fails or restarts the review.

    The sweep runs hourly. Without this, a row whose body no longer matches the
    catalogue would be re-submitted every hour — failing every hour while Meta
    holds it PENDING, and, on the reading where the edit lands, pushing the
    approval further out exactly as often as we asked for it.
    """
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={"template": {"meta_template_id": "M1", "status": "pending",
                            "body_hash": "una-copia-vecchia"}},
    )

    result = await wo.ensure_templates(
        shop_id=SHOP, settings=FakeSettings(),
        approved={("it", k) for k in wt.CATALOGUE},
    )

    assert not calls.get("edit_template"), "Meta refuses this, hourly"
    assert not calls.get("create_template")
    assert result["created"] == 0 and result["edited"] == 0


@pytest.mark.asyncio
async def test_drift_is_deferred_not_dropped_once_meta_has_ruled(monkeypatch):
    """The same row, now approved, is edited on the next sweep."""
    calls = _patch_onboarding(
        monkeypatch, sender={"shop_id": SHOP, "source": "coexistence",
                             "status": "online", "display_name": "Salone X",
                             "waba_id": "WABA1", "access_token": "tok"},
        calls={"template": {"meta_template_id": "M1", "status": "approved",
                            "body_hash": "una-copia-vecchia"}},
    )

    result = await wo.ensure_templates(
        shop_id=SHOP, settings=FakeSettings(),
        approved={("it", k) for k in wt.CATALOGUE},
    )

    assert result["edited"] == len(wt.CATALOGUE)


@pytest.mark.asyncio
async def test_send_loop_survives_a_failed_run(monkeypatch):
    """One bad run (DB blip, Meta outage) must not kill the loop for the
    lifetime of the machine."""
    import asyncio
    calls = []

    async def _send_due(*, settings):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db blip")
        if len(calls) == 3:
            raise asyncio.CancelledError
        return {"sent": 0}

    monkeypatch.setattr(ws, "send_due", _send_due)

    class S(FakeSettings):
        whatsapp_send_loop_seconds = 0

    with pytest.raises(asyncio.CancelledError):
        await ws.send_loop(settings=S())
    assert len(calls) == 3
