"""One answer to "is this shop low on credit", read by every surface.

The owner decided (2026-09-29) on a single threshold per shop —
`shop_config.auto_topup_threshold_tokens`, 10 000 when unset — and the banner,
the email, the WhatsApp responder and the webapp all read that same number.
These tests pin the default and the boundary, because two surfaces disagreeing
by one credit is an owner told "paused" by one screen and "fine" by another.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from booking_engine.services import credit_state as cs

SHOP = uuid4()


@pytest.fixture
def wired(monkeypatch):
    state = {"balance": 50_000, "config": {"auto_topup_threshold_tokens": None}}

    async def balance(shop_id):
        return state["balance"]

    async def config(shop_id):
        return state["config"]

    monkeypatch.setattr(cs, "get_balance", balance)
    monkeypatch.setattr(cs.config_q, "get_config", config)
    return state


async def test_an_unset_threshold_is_the_ten_thousand_default(wired):
    wired["balance"] = 10_001
    assert await cs.credit_state(SHOP) == {
        "balance": 10_001, "threshold": 10_000, "low": False}


async def test_no_config_row_at_all_is_the_default_too(wired):
    wired["config"] = None
    wired["balance"] = 9_999
    assert await cs.credit_state(SHOP) == {
        "balance": 9_999, "threshold": 10_000, "low": True}


async def test_the_threshold_itself_is_low(wired):
    """`balance <= threshold`: at exactly the threshold the responder pauses."""
    wired["config"] = {"auto_topup_threshold_tokens": 2_000}
    wired["balance"] = 2_000
    assert (await cs.credit_state(SHOP))["low"] is True
    wired["balance"] = 2_001
    assert (await cs.credit_state(SHOP))["low"] is False


async def test_an_explicit_zero_threshold_still_pauses_on_an_empty_basket(wired):
    """Zero is a value, not "unset" — it must not fall back to 10 000. And an
    empty basket is low under any threshold: nothing can be paid for."""
    wired["config"] = {"auto_topup_threshold_tokens": 0}
    wired["balance"] = 1
    assert await cs.credit_state(SHOP) == {"balance": 1, "threshold": 0, "low": False}
    wired["balance"] = 0
    assert (await cs.credit_state(SHOP))["low"] is True


def test_is_low_is_the_one_comparison():
    assert cs.is_low(balance=10, threshold=10) is True
    assert cs.is_low(balance=11, threshold=10) is False
