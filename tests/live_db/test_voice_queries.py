"""Live-DB tests for voice_agent queries."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from booking_engine.db.connection import init_connection, close_connection, execute_void
from booking_engine.config import Settings
from booking_engine.db import voice_queries as vq

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for live-DB tests",
)


@pytest.fixture(scope="module", autouse=True)
async def db_pool():
    await init_connection(Settings(database_url=os.environ["DATABASE_URL"]))
    yield
    await close_connection()


@pytest.fixture
async def seeded_shop():
    shop_id = uuid4()
    await execute_void(
        "INSERT INTO business_app_core.shops (id, name, is_active) "
        "VALUES ($1, 'Test Shop', true)",
        shop_id,
    )
    yield shop_id
    await execute_void("DELETE FROM voice_agent.calls WHERE shop_id = $1", shop_id)
    await execute_void("DELETE FROM business_app_core.shops WHERE id = $1", shop_id)


async def test_get_voice_config_returns_defaults(seeded_shop):
    cfg = await vq.get_voice_config(seeded_shop)
    assert cfg["voice"] == "alloy"
    assert cfg["language"] == "it"
    assert cfg["is_active"] is True


async def test_get_voice_config_missing_shop_returns_none():
    cfg = await vq.get_voice_config(uuid4())
    assert cfg is None


async def test_update_voice_config_partial(seeded_shop):
    updated = await vq.update_voice_config(
        seeded_shop, {"welcome_message": "Ciao!", "voice": "echo"}
    )
    assert updated["welcome_message"] == "Ciao!"
    assert updated["voice"] == "echo"
    assert updated["language"] == "it"  # untouched


async def test_update_voice_config_empty_payload_is_noop(seeded_shop):
    before = await vq.get_voice_config(seeded_shop)
    after = await vq.update_voice_config(seeded_shop, {})
    assert before == after


async def test_list_calls_empty(seeded_shop):
    result = await vq.list_calls(seeded_shop, filters={}, cursor=None, limit=20)
    assert result == {"items": [], "next_cursor": None}


async def _insert_call(shop_id: UUID, *, started: datetime, outcome: str | None = None,
                       caller: str = "+39000", customer_match: str = "unmatched") -> UUID:
    cid = uuid4()
    await execute_void(
        "INSERT INTO voice_agent.calls "
        "(id, shop_id, caller_number, customer_match, started_at, ended_at, "
        " duration_seconds, outcome, summary) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        cid, shop_id, caller, customer_match, started,
        started + timedelta(minutes=2), 120, outcome,
        f"summary for {cid}",
    )
    return cid


async def test_list_calls_pagination_and_filter(seeded_shop):
    now = datetime.now(timezone.utc)
    ids = []
    for i in range(5):
        ids.append(await _insert_call(seeded_shop, started=now - timedelta(hours=i),
                                       outcome="booked" if i % 2 == 0 else "abandoned"))
    page1 = await vq.list_calls(seeded_shop, filters={"outcome": ["booked"]},
                                cursor=None, limit=2)
    assert len(page1["items"]) == 2
    assert all(c["outcome"] == "booked" for c in page1["items"])


async def test_get_call_detail_includes_transcript_and_events(seeded_shop):
    now = datetime.now(timezone.utc)
    call_id = await _insert_call(seeded_shop, started=now, outcome="booked")
    await execute_void(
        "INSERT INTO voice_agent.call_transcripts (call_id, turn_index, role, text, at) "
        "VALUES ($1, 0, 'assistant', 'Ciao', $2), ($1, 1, 'caller', 'Ciao!', $2)",
        call_id, now,
    )
    await execute_void(
        "INSERT INTO voice_agent.call_events (call_id, type, payload) "
        "VALUES ($1, 'function_call', '{\"name\": \"book\"}'::jsonb)",
        call_id,
    )
    detail = await vq.get_call_detail(seeded_shop, call_id)
    assert detail["call"]["id"] == call_id
    assert len(detail["transcript"]) == 2
    assert detail["transcript"][0]["role"] == "assistant"
    assert len(detail["events"]) == 1


async def test_get_call_detail_wrong_shop_returns_none(seeded_shop):
    now = datetime.now(timezone.utc)
    call_id = await _insert_call(seeded_shop, started=now)
    other_shop = uuid4()
    assert await vq.get_call_detail(other_shop, call_id) is None


async def test_link_customer_to_call(seeded_shop):
    now = datetime.now(timezone.utc)
    call_id = await _insert_call(seeded_shop, started=now, customer_match="unmatched")
    cust = uuid4()
    await execute_void(
        "INSERT INTO business_app_core.customers (id, shop_id, full_name) "
        "VALUES ($1, $2, 'Mario')",
        cust, seeded_shop,
    )
    updated = await vq.link_customer(seeded_shop, call_id, cust)
    assert updated["customer_id"] == cust
    assert updated["customer_match"] == "existing"


async def test_get_analytics_empty(seeded_shop):
    a = await vq.get_analytics(seeded_shop, from_dt=None, to_dt=None)
    assert a["volume"]["total"] == 0
    assert a["outcomes"]["conversion_rate"] == 0.0
    assert a["demand"]["after_hours_pct"] == 0.0


async def test_get_analytics_counts(seeded_shop):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    await _insert_call(seeded_shop, started=now, outcome="booked")
    await _insert_call(seeded_shop, started=now - timedelta(hours=1), outcome="abandoned")
    await _insert_call(seeded_shop, started=now - timedelta(hours=2), outcome="failed")
    a = await vq.get_analytics(seeded_shop, from_dt=None, to_dt=None)
    assert a["volume"]["total"] == 3
    assert a["outcomes"]["booked"] == 1
    assert a["outcomes"]["abandoned"] == 1
    assert a["outcomes"]["failed"] == 1
    assert a["outcomes"]["conversion_rate"] == pytest.approx(0.5)
    assert a["volume"]["failure_rate"] == pytest.approx(1 / 3)
