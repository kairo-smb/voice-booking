"""A WhatsApp conversation is a session row on voice_agent.calls.

The point of these tests is what they *don't* cover: there is no booking logic
here, no authz, no constraints. Opening a session mints the same call token the
voice path mints, and the twelve existing tools answer it unchanged — the last
test proves that end of it by driving `execute_tool` for real.

The two statements `open_session` issues are exercised against a fake that
applies their predicate, and against a real Postgres separately (the scratch-DB
run in the task report) — a fake cannot tell us whether `$3::interval` binds.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from booking_engine.api.app import create_app
from booking_engine.db import wa_session_queries as ws
from booking_engine.services.call_token import mint_call_token
from booking_engine.services.messaging import wa_routing

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
SHOP = uuid4()
OTHER_SHOP = uuid4()
PHONE = "+393331112222"


class FakeCalls:
    """voice_agent.calls as a list, running open_session's own two statements.

    The SELECT's predicate is applied here rather than asserted as a string so
    the reuse/gap/tenancy tests are about behaviour. The SQL text itself is
    checked against a real server, not against this.
    """

    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []
        self.statements: list[tuple[str, tuple]] = []

    async def execute_one(self, sql: str, *args):
        self.statements.append((sql, args))
        if sql.lstrip().upper().startswith("SELECT"):
            shop_id, phone, gap = args
            hits = [
                r for r in self.rows
                if r["shop_id"] == shop_id
                and r["channel"] == "whatsapp"
                and r["caller_number"] == phone
                and r["ended_at"] is None
                and r["started_at"] > NOW - gap
            ]
            hits.sort(key=lambda r: r["started_at"], reverse=True)
            return {"id": hits[0]["id"]} if hits else None
        shop_id, phone, customer_id, match = args
        row = {"id": uuid4(), "shop_id": shop_id, "channel": "whatsapp",
               "caller_number": phone, "customer_id": customer_id,
               "customer_match": match, "started_at": NOW, "ended_at": None,
               "duration_seconds": None}
        self.rows.append(row)
        return {"id": row["id"]}

    @property
    def inserts(self) -> list[tuple[str, tuple]]:
        return [s for s in self.statements
                if s[0].lstrip().upper().startswith("INSERT")]


def session_row(*, shop_id=SHOP, phone=PHONE, started_at=NOW,
                ended_at=None, channel="whatsapp") -> dict:
    return {"id": uuid4(), "shop_id": shop_id, "channel": channel,
            "caller_number": phone, "customer_id": None,
            "customer_match": "unmatched", "started_at": started_at,
            "ended_at": ended_at, "duration_seconds": None}


@pytest.fixture
def db(monkeypatch):
    fake = FakeCalls()
    monkeypatch.setattr(ws, "execute_one", fake.execute_one)
    return fake


# --- the row --------------------------------------------------------------

@pytest.mark.asyncio
async def test_opening_a_session_writes_a_whatsapp_call_row(db):
    call_id = await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None)

    row = db.rows[0]
    assert row["id"] == call_id
    assert row["channel"] == "whatsapp"
    assert row["caller_number"] == PHONE
    # Voice-only, and migration 24's COMMENT says so. A WhatsApp session that
    # invented a duration would poison every average the webapp computes.
    assert row["duration_seconds"] is None


@pytest.mark.asyncio
async def test_a_known_customer_is_recorded_as_existing(db):
    customer = uuid4()
    await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=customer)

    assert db.rows[0]["customer_id"] == customer
    assert db.rows[0]["customer_match"] == "existing"


@pytest.mark.asyncio
async def test_an_unknown_number_is_recorded_as_unmatched(db):
    await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None)

    # 'unmatched', not 'created': the session did not make a customer, and the
    # legal values are the CHECK in 03_voice_agent_schema.sql, not a guess.
    assert db.rows[0]["customer_match"] == "unmatched"


# --- what counts as one conversation --------------------------------------

@pytest.mark.asyncio
async def test_a_second_message_reuses_the_open_session(db):
    first = await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None)
    second = await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None)

    assert second == first
    assert len(db.rows) == 1
    assert len(db.inserts) == 1


@pytest.mark.asyncio
async def test_a_session_past_the_24h_gap_is_a_new_one(monkeypatch):
    stale = session_row(started_at=NOW - wa_routing.SESSION_GAP - timedelta(minutes=1))
    fake = FakeCalls([stale])
    monkeypatch.setattr(ws, "execute_one", fake.execute_one)

    call_id = await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None)

    assert call_id != stale["id"]
    assert len(fake.rows) == 2


@pytest.mark.asyncio
async def test_the_session_gap_is_wa_routings_constant_not_a_second_literal(db):
    await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None)

    select_sql, select_args = db.statements[0]
    # By reference, not by value: two definitions of "one conversation" that
    # can drift is exactly the bug this avoids. `session_messages` splits a
    # history on this boundary; the row must agree with it by construction.
    assert select_args[2] is wa_routing.SESSION_GAP
    assert "interval" in select_sql


@pytest.mark.asyncio
async def test_a_closed_session_is_not_reused(monkeypatch):
    closed = session_row(ended_at=NOW - timedelta(minutes=5))
    fake = FakeCalls([closed])
    monkeypatch.setattr(ws, "execute_one", fake.execute_one)

    call_id = await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None)

    # ended_at is the agent saying the request is finished. A follow-up
    # message is a new request, not a resumption of one already answered.
    assert call_id != closed["id"]
    assert len(fake.rows) == 2


@pytest.mark.asyncio
async def test_a_voice_call_from_the_same_number_is_not_a_whatsapp_session(monkeypatch):
    voice = session_row(channel="voice")
    fake = FakeCalls([voice])
    monkeypatch.setattr(ws, "execute_one", fake.execute_one)

    call_id = await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None)

    assert call_id != voice["id"]


@pytest.mark.asyncio
async def test_two_shops_with_the_same_number_get_separate_sessions(db):
    a = await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None)
    b = await ws.open_session(shop_id=OTHER_SHOP, phone=PHONE, customer_id=None)

    # Phone numbers are not globally unique across tenants — one person really
    # can be a customer of two salons. A shared session would hand shop B's
    # agent a token scoped to shop A.
    assert a != b
    assert len(db.rows) == 2


# --- the whole point ------------------------------------------------------

@pytest.mark.asyncio
async def test_the_minted_token_authorises_the_booking_tools(db, monkeypatch):
    """Nothing about booking is rebuilt: the existing tool layer answers."""
    monkeypatch.setenv("VOICE_AGENT_TOOL_SECRET", "tool-secret")
    from booking_engine.services.mcp_tools import execute_tool

    call_id = await ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None)
    token = mint_call_token(shop_id=SHOP, call_id=call_id, secret="tool-secret")

    rows = [{"id": uuid4(), "name": "Taglio", "duration_min": 30,
             "price_cents": 2500}]
    with patch("booking_engine.api.routes.voice_tools_catalog.list_services",
               new=AsyncMock(return_value=rows)):
        resp = await execute_tool(
            "get_services", {}, token=token, secret="tool-secret",
            app=create_app(),
        )

    assert resp["ok"] is True
    assert resp["data"][0]["name"] == "Taglio"


def test_open_session_returns_a_uuid(db):
    # The token is minted from it, so a str here would sign a different claim
    # shape than the voice path does.
    import asyncio
    call_id = asyncio.run(ws.open_session(shop_id=SHOP, phone=PHONE, customer_id=None))
    assert isinstance(call_id, UUID)
