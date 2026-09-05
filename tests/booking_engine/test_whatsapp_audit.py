"""The WABA action audit (`whatsapp.audit_events`, migration 20).

Two invariants worth pinning:

- `record_audit_event` is fail-open — a dead audit DB must never break the
  send it logs.
- The route hooks write who-did-what with the actor the webapp propagates,
  and that same actor lands on the per-message send trail (`initiated_by`).
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from booking_engine.db import whatsapp_audit_queries as waq


class FakeSettings:
    whatsapp_send_start_hour = 9
    whatsapp_send_end_hour = 20
    whatsapp_sends_per_minute = 0
    whatsapp_recipient_cooldown_hours = 168


@pytest.mark.asyncio
async def test_record_audit_event_inserts_actor_and_json(monkeypatch):
    calls: list[tuple[str, tuple]] = []

    async def fake_execute_void(sql: str, *args):
        calls.append((sql, args))

    monkeypatch.setattr(waq, "execute_void", fake_execute_void)
    shop, staff = uuid4(), uuid4()

    await waq.record_audit_event(
        shop_id=shop, event="campaign.enqueue", actor_id=staff, source="composer",
        campaign_key="c1", template_name="promo_v1", is_template=True,
        recipient_count=2, status="success",
        request={"campaign_key": "c1"}, response={"ok": True},
    )

    assert len(calls) == 1
    sql, args = calls[0]
    assert "whatsapp.audit_events" in sql
    # $1 shop_id, $2 actor_id, $4 event, $12 request, $13 response
    assert args[0] == shop and args[1] == staff and args[3] == "campaign.enqueue"
    assert json.loads(args[11]) == {"campaign_key": "c1"}
    assert json.loads(args[12]) == {"ok": True}


@pytest.mark.asyncio
async def test_record_audit_event_is_fail_open(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("audit db down")

    monkeypatch.setattr(waq, "execute_void", boom)

    # Must not raise — the audit can never take the action it logs down.
    await waq.record_audit_event(shop_id=uuid4(), event="campaign.enqueue")


@pytest.mark.asyncio
async def test_campaign_enqueue_hook_records_actor(monkeypatch):
    from booking_engine.api.routes import whatsapp as wa_routes
    from booking_engine.api.routes.whatsapp import CampaignRequest

    audit_calls: list[dict] = []
    enqueue_kwargs: dict = {}

    async def fake_enqueue(**kw):
        enqueue_kwargs.update(kw)
        return {"ok": True, "queued": 1, "suppressed": 0, "already_sent": 0,
                "first_at": None, "last_at": None}

    async def fake_audit(**kw):
        audit_calls.append(kw)

    monkeypatch.setattr(wa_routes, "enqueue_campaign", fake_enqueue)
    monkeypatch.setattr(wa_routes.waq, "record_audit_event", fake_audit)

    shop, staff = uuid4(), uuid4()
    payload = CampaignRequest(
        shop_id=shop, requested_by=staff, source="composer",
        campaign_key="c1", template_key="promo_v1",
        recipients=[{"customer_id": uuid4(), "variables": {"1": "Giulia"}}],
    )

    await wa_routes.campaign(payload=payload, settings=FakeSettings(), _auth=True)

    # The actor flows both onto the per-message trail and into the audit event.
    assert enqueue_kwargs["initiated_by"] == staff
    event = audit_calls[0]
    assert event["event"] == "campaign.enqueue"
    assert event["actor_id"] == staff
    assert event["source"] == "composer"
    assert event["campaign_key"] == "c1"
    assert event["is_template"] is True
    assert event["recipient_count"] == 1
    assert event["status"] == "success"
