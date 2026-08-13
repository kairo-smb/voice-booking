import pytest
from uuid import uuid4

from booking_engine.db import voice_telephony_queries as q


@pytest.mark.asyncio
async def test_insert_telephony_does_not_overwrite_an_existing_number(monkeypatch):
    """The PK guarantees one row; nothing guaranteed one PURCHASE.

    A second insert must lose, not overwrite — an overwritten row leaves the
    previously bought number billed by Twilio forever with nothing referencing
    it. The caller uses the None return as its signal to hand the number back.
    """
    captured = {}

    async def fake_execute_one(sql, *args):
        captured["sql"] = sql
        return None            # simulate: a row already existed

    monkeypatch.setattr(q, "execute_one", fake_execute_one)

    row = await q.insert_telephony(
        shop_id=uuid4(), provider="twilio", kairo_number="+37251234567",
        kairo_number_sid="PN1", salon_existing_number=None, setup_path="new",
    )

    assert row is None
    assert "DO NOTHING" in captured["sql"], "must not overwrite an existing row"
    assert "DO UPDATE" not in captured["sql"]


@pytest.mark.asyncio
async def test_insert_telephony_returns_the_row_when_it_wins(monkeypatch):
    async def fake_execute_one(sql, *args):
        return {"shop_id": args[0], "kairo_number": args[2]}
    monkeypatch.setattr(q, "execute_one", fake_execute_one)

    shop = uuid4()
    row = await q.insert_telephony(
        shop_id=shop, provider="twilio", kairo_number="+37251234567",
        kairo_number_sid="PN1", salon_existing_number=None, setup_path="new",
    )
    assert row["shop_id"] == shop


def test_upsert_telephony_still_exists_for_status_updates():
    """upsert_telephony is NOT removed — legitimate callers update an existing
    row's activation status. Only the provisioning path becomes insert-only."""
    assert hasattr(q, "upsert_telephony")
