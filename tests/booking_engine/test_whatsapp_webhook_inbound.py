"""The inbound webhook: dedup, echoes from the owner's phone, button taps.

Hermetic — no Postgres. `FakeDB` stands in for the only two tables this path
writes and is deliberately strict about the one property a plain mock would
hide: `inbound_messages_wa_id_uniq` is a **partial** index, so an INSERT whose
ON CONFLICT clause omits the predicate is rejected here the way Postgres
rejects it ("no unique or exclusion constraint matching..."). This repo has
shipped that exact inference failure twice — AGENTS.md 2026-07-18, 2026-07-21.

The webhook's posture is load-bearing and asserted throughout: a genuine
request always gets a 200. Meta retries anything else and disables a webhook
that keeps failing, which would silently cost every delivery status and every
opt-out.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from booking_engine.api.app import create_app
from booking_engine.api.routes import whatsapp as wa_routes
from booking_engine.db import whatsapp_queries as wq

SHOP = uuid4()
WABA = "WABA-1"
APP_SECRET = "app-secret"
CUSTOMER_PHONE = "393331112222"      # Meta sends wa_ids without a '+'
BUSINESS_PHONE = "393401110000"


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- fake DB

class FakeDB:
    """Just enough Postgres for the two tables the webhook writes.

    Rows are built by unpacking the query's positional arguments in order, so
    reordering a statement's parameters without updating its caller fails here
    loudly instead of writing a body into a phone column.
    """

    def __init__(self):
        self.inbound: list[dict] = []
        self.outbound: list[dict] = []
        self.statements: list[str] = []

    # -- the three statements this path issues -----------------------------

    async def execute_one(self, sql, *args):
        self.statements.append(sql)
        if "INSERT INTO whatsapp.inbound_messages" in sql:
            return self._insert_inbound(sql, args)
        if "INSERT INTO whatsapp.outbound_messages" in sql:
            return self._insert_echo(sql, args)
        raise AssertionError(f"unexpected execute_one: {sql}")

    async def execute_void(self, sql, *args):
        self.statements.append(sql)
        if "UPDATE whatsapp.inbound_messages" in sql and "read_at" in sql:
            return self._clear_unread(args)
        raise AssertionError(f"unexpected execute_void: {sql}")

    async def execute(self, sql, *args):
        self.statements.append(sql)
        raise AssertionError(f"unexpected execute: {sql}")

    # -- statement bodies ---------------------------------------------------

    def _insert_inbound(self, sql, args):
        assert "ON CONFLICT (wa_message_id) WHERE wa_message_id IS NOT NULL" in sql, (
            "inbound_messages_wa_id_uniq is a PARTIAL index: Postgres cannot "
            "infer it unless the ON CONFLICT clause repeats its predicate"
        )
        shop_id, from_phone, body, message_type, wa_message_id, intent, confidence = args
        # The partial index: NULL ids never conflict with anything.
        if wa_message_id is not None and any(
            r["wa_message_id"] == wa_message_id for r in self.inbound
        ):
            return None                      # DO NOTHING -> RETURNING no row
        row = {
            "id": uuid4(), "shop_id": shop_id, "from_phone": from_phone,
            "body": body, "message_type": message_type,
            "wa_message_id": wa_message_id, "intent": intent,
            "confidence": confidence, "read_at": None, "received_at": _now(),
        }
        self.inbound.append(row)
        return dict(row)

    def _insert_echo(self, sql, args):
        shop_id, to_phone, from_number, preview, provider_sid = args
        if provider_sid and any(
            r["provider_sid"] == provider_sid for r in self.outbound
        ):
            return None
        row = {
            "id": uuid4(), "shop_id": shop_id, "to_phone": to_phone,
            "from_number": from_number, "preview": preview,
            "provider_sid": provider_sid, "origin": "phone", "status": "sent",
            "template_name": None, "campaign_key": None, "sent_at": _now(),
        }
        self.outbound.append(row)
        return dict(row)

    def _clear_unread(self, args):
        shop_id, phone = args
        for row in self.inbound:
            if (row["shop_id"] == shop_id
                    and row["from_phone"].lstrip("+") == str(phone).lstrip("+")
                    and row["read_at"] is None):
                row["read_at"] = _now()

    # -- what the tests ask it ---------------------------------------------

    def window_expires_in_hours(self, phone=CUSTOMER_PHONE) -> float | None:
        """The 24h service window: max(received_at) over *customer* inbound."""
        received = [r["received_at"] for r in self.inbound
                    if r["from_phone"].lstrip("+") == phone.lstrip("+")]
        if not received:
            return None
        return (max(received) + timedelta(hours=24) - _now()).total_seconds() / 3600

    def unread_count(self, phone=CUSTOMER_PHONE) -> int:
        return sum(1 for r in self.inbound
                   if r["from_phone"].lstrip("+") == phone.lstrip("+")
                   and r["read_at"] is None)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(wq, "execute_one", fake.execute_one)
    monkeypatch.setattr(wq, "execute_void", fake.execute_void)
    monkeypatch.setattr(wq, "execute", fake.execute)

    async def _sender(waba_id):
        return {"shop_id": SHOP, "waba_id": waba_id, "phone_number": BUSINESS_PHONE}
    monkeypatch.setattr(wa_routes.wq, "get_sender_by_waba", _sender)
    return fake


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", APP_SECRET)
    return TestClient(create_app())


# ------------------------------------------------------------------- payloads

def _envelope(field: str, value: dict) -> dict:
    return {"object": "whatsapp_business_account",
            "entry": [{"id": WABA, "changes": [{"field": field, "value": value}]}]}


def inbound_payload(wa_id: str | None = "wamid.A", body: str = "ciao", **over) -> dict:
    message = {"from": CUSTOMER_PHONE, "timestamp": "1", "type": "text",
               "text": {"body": body}}
    if wa_id is not None:
        message["id"] = wa_id
    message.update(over)
    return _envelope("messages", {"messaging_product": "whatsapp",
                                  "messages": [message]})


def echo_payload(to: str = CUSTOMER_PHONE, body: str = "certo, alle 15",
                 wa_id: str = "wamid.E", field: str = "smb_message_echoes") -> dict:
    """Meta's coexistence echo. `from` is the business, `to` is the customer."""
    return _envelope(field, {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": BUSINESS_PHONE,
                     "phone_number_id": "PN1"},
        "message_echoes": [{"from": BUSINESS_PHONE, "to": to, "id": wa_id,
                            "timestamp": "1", "type": "text",
                            "text": {"body": body}}],
    })


def interactive_payload(button_id: str = "booking", title: str = "Prenotare",
                        kind: str = "button_reply", wa_id: str = "wamid.I") -> dict:
    return _envelope("messages", {
        "messaging_product": "whatsapp",
        "messages": [{"from": CUSTOMER_PHONE, "id": wa_id, "timestamp": "1",
                      "type": "interactive",
                      "interactive": {"type": kind,
                                      kind: {"id": button_id, "title": title}}}],
    })


def post_webhook(client, payload: dict):
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(
        APP_SECRET.encode(), raw, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/api/v1/whatsapp/webhook", content=raw,
        headers={"X-Hub-Signature-256": signature,
                 "Content-Type": "application/json"},
    )


# ------------------------------------------------------------------ dedup

def test_a_replayed_webhook_does_not_duplicate_the_message(client, db):
    """Meta retries. Without the dedup key a retry is a second bubble in the
    thread and, later, a second AI classification that costs real money."""
    payload = inbound_payload(wa_id="wamid.A", body="ciao")

    assert post_webhook(client, payload).status_code == 200
    assert post_webhook(client, payload).status_code == 200

    assert [r["wa_message_id"] for r in db.inbound] == ["wamid.A"]
    assert len(db.inbound) == 1


@pytest.mark.asyncio
async def test_a_replay_returns_no_row_so_the_caller_can_skip_the_work(db):
    """The dedup and "have we already processed this?" are one question.

    `record_inbound` answers it once by returning None on a replay — that is
    what a later stage reads to skip transcription and classification.
    """
    first = await wq.record_inbound(
        shop_id=SHOP, from_phone=CUSTOMER_PHONE, body="ciao",
        message_type="text", wa_message_id="wamid.Z",
    )
    second = await wq.record_inbound(
        shop_id=SHOP, from_phone=CUSTOMER_PHONE, body="ciao",
        message_type="text", wa_message_id="wamid.Z",
    )

    assert first is not None and first["body"] == "ciao"
    assert second is None


def test_a_message_with_no_id_still_records(client, db):
    """The column is nullable and the index partial for exactly this reason."""
    assert post_webhook(client, inbound_payload(wa_id=None, body="uno")).status_code == 200

    assert len(db.inbound) == 1
    assert db.inbound[0]["wa_message_id"] is None
    assert db.inbound[0]["body"] == "uno"


def test_two_messages_with_no_id_do_not_collapse_into_one(client, db):
    post_webhook(client, inbound_payload(wa_id=None, body="uno"))
    post_webhook(client, inbound_payload(wa_id=None, body="due"))

    assert [r["body"] for r in db.inbound] == ["uno", "due"]


def test_the_inbound_insert_repeats_the_partial_index_predicate():
    """Asserted on the SQL itself, not just through the fake.

    `ON CONFLICT (wa_message_id) DO NOTHING` cannot infer a partial index and
    fails outright at runtime — the statement never runs, so the message is
    lost and the webhook 500s back to Meta.
    """
    import inspect
    source = inspect.getsource(wq.record_inbound)

    assert "ON CONFLICT (wa_message_id) WHERE wa_message_id IS NOT NULL" in source
    assert "RETURNING *" in source


# ------------------------------------------------------------------- echoes

def test_an_echo_is_recorded_as_outbound_from_the_phone(client, db):
    """Every salon is coexistence: the owner answers from the Business App and
    Meta reports it here. Without it the thread is half a conversation."""
    assert post_webhook(client, echo_payload(body="certo, alle 15")).status_code == 200

    assert len(db.outbound) == 1
    row = db.outbound[0]
    assert row["origin"] == "phone"
    assert row["status"] == "sent"
    assert row["preview"] == "certo, alle 15"
    assert row["template_name"] is None
    assert row["campaign_key"] is None
    assert row["to_phone"] == CUSTOMER_PHONE


def test_an_echo_does_not_extend_the_service_window(client, db):
    """The window is driven by customer inbound alone. An echo that extended
    it would let us send into a conversation Meta considers closed, which
    comes back as an opaque provider error long after the cause."""
    db.inbound.append({
        "id": uuid4(), "shop_id": SHOP, "from_phone": CUSTOMER_PHONE,
        "body": "ci sei?", "message_type": "text", "wa_message_id": "wamid.old",
        "intent": None, "confidence": None, "read_at": None,
        "received_at": _now() - timedelta(hours=23),
    })

    post_webhook(client, echo_payload())

    assert db.window_expires_in_hours() == pytest.approx(1.0, abs=0.1)
    # And nothing inbound was written at all — the window has no other input.
    assert len(db.inbound) == 1


def test_an_echo_clears_the_unread_state(client, db):
    """The owner already answered; the Inbox must not keep asking them to."""
    db.inbound.append({
        "id": uuid4(), "shop_id": SHOP, "from_phone": CUSTOMER_PHONE,
        "body": "ci sei?", "message_type": "text", "wa_message_id": "wamid.old",
        "intent": None, "confidence": None, "read_at": None,
        "received_at": _now() - timedelta(minutes=5),
    })
    assert db.unread_count() == 1

    post_webhook(client, echo_payload())

    assert db.unread_count() == 0


def test_an_echo_for_a_phone_with_no_prior_inbound_does_not_crash(client, db):
    r = post_webhook(client, echo_payload(to="393339998887"))

    assert r.status_code == 200
    assert db.outbound[0]["to_phone"] == "393339998887"
    assert db.inbound == []


def test_a_replayed_echo_is_not_duplicated(client, db):
    payload = echo_payload(wa_id="wamid.E1")

    post_webhook(client, payload)
    post_webhook(client, payload)

    assert len(db.outbound) == 1


def test_an_echo_with_no_text_is_still_recorded(client, db):
    """An image or a voice note sent from the owner's phone carries no body.
    Dropping it would leave a gap in the thread with nothing explaining it."""
    payload = echo_payload()
    payload["entry"][0]["changes"][0]["value"]["message_echoes"][0] = {
        "from": BUSINESS_PHONE, "to": CUSTOMER_PHONE, "id": "wamid.IMG",
        "type": "image", "image": {"id": "MEDIA1"},
    }

    assert post_webhook(client, payload).status_code == 200
    assert db.outbound[0]["preview"] == ""


# -------------------------------------------------------------- interactive

def test_a_button_tap_is_stored_with_its_id_as_the_intent(client, db):
    """The id is one we defined, so it *is* the intent — no model call, no
    cost, and no possibility of a hallucinated verdict."""
    assert post_webhook(client, interactive_payload(button_id="booking")).status_code == 200

    row = db.inbound[0]
    assert row["intent"] == "booking"
    assert float(row["confidence"]) == 1.0
    assert row["body"] == "Prenotare"        # the title, for the thread view


def test_a_list_tap_is_read_like_a_button_tap(client, db):
    post_webhook(client, interactive_payload(
        button_id="prices", title="Listino", kind="list_reply"))

    assert db.inbound[0]["intent"] == "prices"
    assert db.inbound[0]["body"] == "Listino"


def test_a_typed_message_carries_no_intent_and_no_confidence(client, db):
    """Anything typed is for the classifier to rule on, later and elsewhere."""
    post_webhook(client, inbound_payload(body="vorrei un taglio"))

    assert db.inbound[0]["intent"] is None
    assert db.inbound[0]["confidence"] is None


def test_an_interactive_message_with_neither_reply_shape_does_not_crash(client, db):
    payload = interactive_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["interactive"] = {
        "type": "nfm_reply", "nfm_reply": {"response_json": "{}"},
    }

    assert post_webhook(client, payload).status_code == 200
    assert db.inbound[0]["intent"] is None
    assert db.inbound[0]["body"] == ""


# ------------------------------------------------------- the 200, defended

def test_an_unknown_webhook_field_is_ignored_and_still_answers_200(client, db):
    """Meta disables a webhook that keeps failing. A field we do not handle —
    today `history`, tomorrow whatever they add — must be a no-op, not a 500."""
    r = post_webhook(client, _envelope("smb_app_state_sync", {"state": {}}))

    assert r.status_code == 200
    assert db.inbound == [] and db.outbound == []


def test_a_message_missing_its_sender_does_not_raise(client, db):
    payload = inbound_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0].pop("from")

    assert post_webhook(client, payload).status_code == 200
    assert db.inbound[0]["from_phone"] == ""


def test_text_as_a_bare_string_does_not_raise(client, db):
    """Defensive shape the route already had; keep it."""
    assert post_webhook(client, inbound_payload(text="ciao secco")).status_code == 200
    assert db.inbound[0]["body"] == "ciao secco"


def test_an_echo_with_no_recipient_does_not_raise(client, db):
    payload = echo_payload()
    payload["entry"][0]["changes"][0]["value"]["message_echoes"][0].pop("to")

    assert post_webhook(client, payload).status_code == 200


def test_a_failing_event_does_not_take_down_the_rest_of_the_batch(client, db, monkeypatch):
    """One bad event must never cost the delivery statuses next to it."""
    calls = {"n": 0}
    original = wq.record_inbound

    async def _explode_once(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await original(**kw)
    monkeypatch.setattr(wa_routes.wq, "record_inbound", _explode_once)

    payload = inbound_payload(wa_id="wamid.1", body="uno")
    payload["entry"][0]["changes"].append(
        {"field": "messages", "value": {"messages": [
            {"from": CUSTOMER_PHONE, "id": "wamid.2", "type": "text",
             "text": {"body": "due"}}]}})

    r = post_webhook(client, payload)

    assert r.status_code == 200
    assert [row["body"] for row in db.inbound] == ["due"]


def test_an_unsigned_webhook_is_still_refused(client, db):
    """The 200-always rule applies to *genuine* requests only."""
    r = client.post("/api/v1/whatsapp/webhook", json=inbound_payload())

    assert r.status_code == 403
    assert db.inbound == []
