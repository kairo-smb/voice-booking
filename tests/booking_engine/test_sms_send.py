import pytest
from uuid import uuid4

from booking_engine.db import token_basket_queries as tbq

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
