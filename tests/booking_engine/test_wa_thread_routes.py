"""The Inbox's read-and-reply API: list, timeline, free-form reply.

Hermetic — no Postgres, no Graph. The thread queries are stubbed at their own
module (`whatsapp_thread_queries`), not at the route module, so these tests
pin behaviour rather than the alias the routes happen to import under.

Two properties are asserted here more insistently than anything else, because
both fail silently in production:

* **The window is checked before Graph, never after.** Outside 24h of the
  customer's last message Meta answers `131047`, which reaches the owner as an
  opaque provider error. A closed window must produce a named refusal and
  *zero* Graph requests — `fake_meta.sends == []` is the assertion that says so.
* **Nothing here debits AI credits.** Meta bills the salon directly on this
  channel (Tech Provider, no shared credit line), so a debit would charge for
  something nobody charges us for. Both debit paths are replaced by stubs that
  fail the test if they are so much as called.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from booking_engine.api.app import create_app
from booking_engine.clients import meta_whatsapp as meta
from booking_engine.clients import webapp_credits
from booking_engine.db import token_basket_queries as tbq
from booking_engine.db import whatsapp_queries as wq
from booking_engine.db import whatsapp_thread_queries as tq

SHOP = uuid4()
SECRET = "s3cret"
AUTH = {"Authorization": f"Bearer {SECRET}"}
# Meta reports `from` as bare E.164; the webapp holds whatever the customer
# record says, usually with the plus. Both must reach the same thread.
BARE = "393331112222"
PLUS = "+393331112222"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SECRET", SECRET)
    return TestClient(create_app())


# --------------------------------------------------------------------- fakes

class FakeThreads:
    """Stands in for `whatsapp_thread_queries`, normalising phones the way the
    SQL does (`ltrim(phone, '+')` on both sides) so a route that invented its
    own second normalisation would show up as a missed thread here."""

    def __init__(self, *, inbound_at=None, timeline=None, threads=None):
        self.inbound_at = inbound_at
        self.timelines = timeline or {}
        self.threads = threads or []
        self.marked: list[tuple] = []
        self.replies: list[dict] = []
        self.seen_phones: list[str] = []

    @staticmethod
    def key(phone: str) -> str:
        return phone.lstrip("+")

    async def thread_list(self, shop_id):
        return [dict(r) for r in self.threads]

    async def thread_timeline(self, shop_id, phone):
        self.seen_phones.append(phone)
        return list(self.timelines.get(self.key(phone), []))

    async def mark_read(self, shop_id, phone):
        self.marked.append((shop_id, phone))
        return len(self.timelines.get(self.key(phone), []))

    async def last_inbound_at(self, shop_id, phone):
        self.seen_phones.append(phone)
        return self.inbound_at

    async def record_reply(self, *, shop_id, to_phone, body, provider_sid):
        row = {"shop_id": shop_id, "to_phone": to_phone, "body": body,
               "provider_sid": provider_sid, "origin": "kairo",
               "template_name": None, "campaign_key": None}
        self.replies.append(row)
        return row


class FakeMeta:
    def __init__(self, sid="wamid.OUT1"):
        self.sid = sid
        self.sends: list[dict] = []

    async def send_text(self, *, phone_number_id, to, body, token):
        self.sends.append({"phone_number_id": phone_number_id, "to": to,
                           "body": body, "token": token})
        return self.sid


def _sender(**over):
    row = {"shop_id": SHOP, "status": "online", "phone_number_id": "PN1",
           "access_token": "plain-token", "phone_number": PLUS}
    row.update(over)
    return row


def _wire(monkeypatch, *, threads: FakeThreads, fake_meta: FakeMeta | None = None,
          sender=None):
    for name in ("thread_list", "thread_timeline", "mark_read",
                 "last_inbound_at", "record_reply"):
        monkeypatch.setattr(tq, name, getattr(threads, name))
    if fake_meta is not None:
        monkeypatch.setattr(meta, "send_text", fake_meta.send_text)

    async def _get_sender(shop_id):
        return _sender() if sender is None else sender
    monkeypatch.setattr(wq, "get_sender", _get_sender)

    # Neither debit path may be reachable from a reply. Stubs that raise, so a
    # call is a failed test rather than a charge nobody notices.
    async def _no_debit(*a, **kw):
        raise AssertionError("a WhatsApp reply must never debit the basket")
    monkeypatch.setattr(tbq, "try_debit_for_message", _no_debit)
    monkeypatch.setattr(webapp_credits, "charge_actual", _no_debit)
    return threads


def _reply_body(**over):
    payload = {"shop_id": str(SHOP), "phone": PLUS, "body": "Ciao, a domani!"}
    payload.update(over)
    return payload


# ------------------------------------------------------------- the thread list

def test_the_list_computes_the_window_and_attention_per_row(client, monkeypatch):
    rows = [
        # Wrote 25h ago: the window has closed, and the session was never
        # routed, so the owner has to speak — and cannot, free-form.
        {"phone": BARE, "intent": None, "escalated": False,
         "last_inbound": _now() - timedelta(hours=25), "unread": 2},
        # Wrote an hour ago and the router named it: open, and handled.
        {"phone": "393334445555", "intent": "booking", "escalated": False,
         "last_inbound": _now() - timedelta(hours=1), "unread": 0},
    ]
    _wire(monkeypatch, threads=FakeThreads(threads=rows))

    data = client.get(f"/api/v1/whatsapp/threads/{SHOP}", headers=AUTH).json()["data"]

    assert [r["window_open"] for r in data] == [False, True]
    assert [r["needs_attention"] for r in data] == [True, False]


def test_the_list_is_one_query_not_one_per_thread(client, monkeypatch):
    """The Inbox's first screen. N+1 here is N round trips before anything
    renders, which is why the session intent is derived in the list query."""
    calls = {"n": 0}
    threads = FakeThreads(threads=[
        {"phone": f"39333000{i:04d}", "intent": "booking", "escalated": False,
         "last_inbound": _now(), "unread": 0}
        for i in range(25)
    ])
    original = threads.thread_list

    async def counted(shop_id):
        calls["n"] += 1
        return await original(shop_id)
    threads.thread_list = counted  # type: ignore[assignment]
    _wire(monkeypatch, threads=threads)

    r = client.get(f"/api/v1/whatsapp/threads/{SHOP}", headers=AUTH)

    assert len(r.json()["data"]) == 25
    assert calls["n"] == 1
    # Nothing per-row: no timeline read, no window query behind each thread.
    assert threads.seen_phones == []


# ---------------------------------------------------------------- the timeline

def test_reading_a_thread_marks_it_read(client, monkeypatch):
    threads = _wire(monkeypatch, threads=FakeThreads(timeline={
        BARE: [{"direction": "in", "text": "Ciao", "at": _now()}]
    }))

    r = client.get(f"/api/v1/whatsapp/threads/{SHOP}/{PLUS}", headers=AUTH)

    assert r.status_code == 200
    # Reading and marking read are the same act — a separate endpoint is one
    # more call the webapp can forget, and then the badge lies forever.
    assert threads.marked == [(SHOP, PLUS)]


def test_the_timeline_merges_both_directions_in_time_order(client, monkeypatch):
    base = _now() - timedelta(hours=3)
    merged = [
        {"direction": "in", "text": "Ciao", "at": base},
        {"direction": "out", "text": "Buongiorno!", "at": base + timedelta(minutes=5),
         "origin": "kairo"},
        {"direction": "out", "text": "Dimmi pure", "at": base + timedelta(minutes=6),
         "origin": "phone"},
        {"direction": "in", "text": "Domani alle 10?", "at": base + timedelta(minutes=30)},
    ]
    _wire(monkeypatch, threads=FakeThreads(timeline={BARE: merged}))

    data = client.get(f"/api/v1/whatsapp/threads/{SHOP}/{PLUS}",
                      headers=AUTH).json()["data"]

    assert [m["direction"] for m in data["messages"]] == ["in", "out", "out", "in"]
    ats = [m["at"] for m in data["messages"]]
    assert ats == sorted(ats)
    # The owner's own answers from the Business App are badged apart from ours.
    assert [m["origin"] for m in data["messages"] if m["direction"] == "out"] \
        == ["kairo", "phone"]


def test_reading_a_thread_that_does_not_exist_is_empty_not_an_error(client, monkeypatch):
    threads = _wire(monkeypatch, threads=FakeThreads(timeline={}))

    r = client.get(f"/api/v1/whatsapp/threads/{SHOP}/393339998888", headers=AUTH)

    assert r.status_code == 200
    assert r.json()["data"]["messages"] == []
    # Marking read is still attempted and still updates nothing: it is
    # idempotent by construction (`read_at IS NULL`), so there is no reason to
    # branch on emptiness here.
    assert len(threads.marked) == 1


def test_a_plus_and_a_bare_phone_are_the_same_thread(client, monkeypatch):
    """Normalisation lives in exactly one place — `ltrim(phone,'+')` in the
    thread SQL. The routes forward the caller's spelling verbatim; a second
    normalisation here would be a second thing to keep in agreement."""
    threads = _wire(monkeypatch, threads=FakeThreads(timeline={
        BARE: [{"direction": "in", "text": "Ciao", "at": _now()}]
    }))

    with_plus = client.get(f"/api/v1/whatsapp/threads/{SHOP}/{PLUS}", headers=AUTH)
    without = client.get(f"/api/v1/whatsapp/threads/{SHOP}/{BARE}", headers=AUTH)

    assert with_plus.json() == without.json()
    assert len(with_plus.json()["data"]["messages"]) == 1
    # Verbatim, both times: the '+' reaches the query, which is what strips it.
    assert threads.seen_phones == [PLUS, BARE]


# -------------------------------------------------------------------- replying

def test_a_reply_outside_the_window_is_refused_before_any_graph_call(client, monkeypatch):
    fake_meta = FakeMeta()
    _wire(monkeypatch, fake_meta=fake_meta,
          threads=FakeThreads(inbound_at=_now() - timedelta(hours=25)))

    r = client.post("/api/v1/whatsapp/reply", headers=AUTH, json=_reply_body())

    assert r.json() == {"ok": False, "error": "session_window_closed"}
    # The whole point: Meta is never asked. 131047 would come back as an
    # opaque provider error the owner cannot act on.
    assert fake_meta.sends == []


def test_a_thread_nobody_ever_wrote_to_cannot_be_replied_to(client, monkeypatch):
    fake_meta = FakeMeta()
    _wire(monkeypatch, fake_meta=fake_meta, threads=FakeThreads(inbound_at=None))

    r = client.post("/api/v1/whatsapp/reply", headers=AUTH, json=_reply_body())

    assert r.json()["error"] == "session_window_closed"
    assert fake_meta.sends == []


def test_a_reply_inside_the_window_is_sent_and_recorded(client, monkeypatch):
    fake_meta = FakeMeta(sid="wamid.ABC")
    threads = _wire(monkeypatch, fake_meta=fake_meta,
                    threads=FakeThreads(inbound_at=_now() - timedelta(hours=2)))

    r = client.post("/api/v1/whatsapp/reply", headers=AUTH, json=_reply_body())

    assert r.json()["data"] == {"sent": True, "provider_sid": "wamid.ABC"}
    assert fake_meta.sends == [{"phone_number_id": "PN1", "to": PLUS,
                                "body": "Ciao, a domani!", "token": "plain-token"}]
    recorded = threads.replies[0]
    # Neither a template nor a campaign: the campaign idempotency index is
    # partial on both, so the owner may legitimately send the same words twice.
    assert recorded["origin"] == "kairo"
    assert recorded["template_name"] is None
    assert recorded["campaign_key"] is None
    assert recorded["provider_sid"] == "wamid.ABC"


def test_a_reply_never_debits_the_basket(client, monkeypatch):
    """Meta bills the salon directly on this channel — there is no credit line
    to share and no cost to recover. `_wire` installs raising stubs on both
    debit paths; reaching either fails this test."""
    fake_meta = FakeMeta()
    _wire(monkeypatch, fake_meta=fake_meta,
          threads=FakeThreads(inbound_at=_now() - timedelta(minutes=5)))

    r = client.post("/api/v1/whatsapp/reply", headers=AUTH, json=_reply_body())

    assert r.json()["data"]["sent"] is True
    assert len(fake_meta.sends) == 1


def test_a_reply_from_an_offline_sender_is_refused(client, monkeypatch):
    fake_meta = FakeMeta()
    _wire(monkeypatch, fake_meta=fake_meta, sender=_sender(status="pending_signup"),
          threads=FakeThreads(inbound_at=_now()))

    r = client.post("/api/v1/whatsapp/reply", headers=AUTH, json=_reply_body())

    assert r.json() == {"ok": False, "error": "sender_offline"}
    assert fake_meta.sends == []


def test_a_reply_from_a_shop_with_no_sender_at_all_is_refused(client, monkeypatch):
    fake_meta = FakeMeta()
    _wire(monkeypatch, fake_meta=fake_meta, sender=False,
          threads=FakeThreads(inbound_at=_now()))

    async def _none(shop_id):
        return None
    monkeypatch.setattr(wq, "get_sender", _none)

    r = client.post("/api/v1/whatsapp/reply", headers=AUTH, json=_reply_body())

    assert r.json()["error"] == "sender_offline"
    assert fake_meta.sends == []


@pytest.mark.parametrize("body", ["", "   ", "\n\t "])
def test_an_empty_reply_is_refused_here_rather_than_at_meta(client, monkeypatch, body):
    fake_meta = FakeMeta()
    _wire(monkeypatch, fake_meta=fake_meta,
          threads=FakeThreads(inbound_at=_now()))

    r = client.post("/api/v1/whatsapp/reply", headers=AUTH,
                    json=_reply_body(body=body))

    assert r.json() == {"ok": False, "error": "empty_body"}
    assert fake_meta.sends == []


def test_a_send_that_could_not_be_recorded_still_reports_it_was_sent(client, monkeypatch):
    """The customer's phone already has the message. Reporting failure would
    have the owner send it a second time, so the response says `sent: True`
    with `recorded: False` — and the loss is logged, not swallowed."""
    fake_meta = FakeMeta(sid="wamid.XYZ")
    threads = FakeThreads(inbound_at=_now() - timedelta(hours=1))

    async def _boom(**kw):
        raise RuntimeError("db down")
    threads.record_reply = _boom  # type: ignore[assignment]
    _wire(monkeypatch, fake_meta=fake_meta, threads=threads)

    r = client.post("/api/v1/whatsapp/reply", headers=AUTH, json=_reply_body())

    assert r.status_code == 200
    assert r.json()["data"] == {"sent": True, "provider_sid": "wamid.XYZ",
                                "recorded": False}


# ------------------------------------------------------------------------ auth

@pytest.mark.parametrize("method,path,payload", [
    ("get", f"/api/v1/whatsapp/threads/{SHOP}", None),
    ("get", f"/api/v1/whatsapp/threads/{SHOP}/{BARE}", None),
    ("post", "/api/v1/whatsapp/reply", {"shop_id": str(SHOP), "phone": PLUS,
                                        "body": "Ciao"}),
])
def test_every_thread_route_refuses_an_unauthenticated_caller(
    client, monkeypatch, method, path, payload
):
    fake_meta = FakeMeta()
    threads = _wire(monkeypatch, fake_meta=fake_meta,
                    threads=FakeThreads(inbound_at=_now(),
                                        timeline={BARE: [{"direction": "in"}]}))

    r = getattr(client, method)(path, **({"json": payload} if payload else {}))

    assert r.status_code == 401
    # Refused before anything happened: nothing read, nothing marked, nothing sent.
    assert threads.marked == [] and fake_meta.sends == []
