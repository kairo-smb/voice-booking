import pytest
from uuid import uuid4

from booking_engine.services.messaging import sms_inbound

SHOP = uuid4()


def test_stop_keywords_are_recognised_case_and_space_insensitively():
    for raw in ["STOP", "stop", "  Stop  ", "CANCELLA", "ALT", "unsubscribe"]:
        assert sms_inbound.parse_stop_keyword(raw) is not None


def test_ordinary_replies_are_not_opt_outs():
    # "Stop" must match the word, not appear anywhere in a sentence — a customer
    # replying "non fermatevi, stop mai!" has not opted out.
    for raw in ["Grazie mille!", "Va bene per giovedi", "non fermatevi, stop mai!"]:
        assert sms_inbound.parse_stop_keyword(raw) is None


def test_empty_body_is_not_an_opt_out():
    assert sms_inbound.parse_stop_keyword("") is None
    assert sms_inbound.parse_stop_keyword(None) is None


@pytest.mark.asyncio
async def test_stop_writes_both_the_list_and_the_consent_row(monkeypatch):
    recorded, withdrawn = {}, {}

    async def q_shop(number): return SHOP
    async def q_record(**kw): recorded.update(kw)
    async def q_withdraw(**kw):
        withdrawn.update(kw)
        return 1

    monkeypatch.setattr(sms_inbound.sms_queries, "get_shop_by_sender_number", q_shop)
    monkeypatch.setattr(sms_inbound.sms_queries, "record_opt_out", q_record)
    monkeypatch.setattr(sms_inbound.sms_queries, "withdraw_marketing_consent", q_withdraw)

    handled = await sms_inbound.handle_inbound(
        to_number="+37251234567", from_number="+393331234567", body="STOP"
    )

    assert handled is True
    assert recorded["keyword"] == "STOP"
    assert recorded["phone_normalized"] == "+393331234567"
    # Both writes: the list alone would leave the webapp showing consent.
    assert withdrawn["phone_normalized"] == "+393331234567"


@pytest.mark.asyncio
async def test_stop_from_an_unknown_phone_still_suppresses(monkeypatch):
    # No customers row to update — the opt_outs entry must stand on its own.
    recorded = {}

    async def q_shop(number): return SHOP
    async def q_record(**kw): recorded.update(kw)
    async def q_withdraw(**kw): return 0

    monkeypatch.setattr(sms_inbound.sms_queries, "get_shop_by_sender_number", q_shop)
    monkeypatch.setattr(sms_inbound.sms_queries, "record_opt_out", q_record)
    monkeypatch.setattr(sms_inbound.sms_queries, "withdraw_marketing_consent", q_withdraw)

    assert await sms_inbound.handle_inbound(
        to_number="+37251234567", from_number="+393339999999", body="stop"
    ) is True
    assert recorded["phone_normalized"] == "+393339999999"


@pytest.mark.asyncio
async def test_unroutable_number_is_ignored(monkeypatch):
    async def q_shop(number): return None
    monkeypatch.setattr(sms_inbound.sms_queries, "get_shop_by_sender_number", q_shop)

    assert await sms_inbound.handle_inbound(
        to_number="+37259999999", from_number="+393331234567", body="STOP"
    ) is False
