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


# --- the once-per-episode email ----------------------------------------------

class Spy:
    def __init__(self, result=None):
        self.calls: list = []
        self.result = result

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture
def sweep(monkeypatch, wired):
    rows: list[dict] = []

    async def candidates():
        return list(rows)

    notify = Spy(True)
    mark = Spy(None)
    monkeypatch.setattr(cs.config_q, "list_credit_notice_candidates", candidates)
    monkeypatch.setattr(cs.config_q, "set_credit_low_notified", mark)
    monkeypatch.setattr(cs.webapp_notify, "whatsapp_credit_low", notify)
    return {"rows": rows, "notify": notify, "mark": mark, "state": wired}


def _row(**kw):
    return {"shop_id": SHOP, "credit_low_notified_at": None, "eligible": True, **kw}


async def test_a_low_shop_is_mailed_once_and_stamped(sweep):
    sweep["state"]["balance"] = 5_000
    sweep["rows"].append(_row())

    counts = await cs.notify_sweep(settings=object())

    assert counts == {"notified": 1, "cleared": 0, "errors": 0}
    _, kw = sweep["notify"].calls[0]
    assert (kw["shop_id"], kw["balance"], kw["threshold"]) == (SHOP, 5_000, 10_000)
    assert sweep["mark"].calls == [((SHOP,), {"notified": True})]


async def test_a_stamped_low_shop_is_not_mailed_again(sweep):
    sweep["state"]["balance"] = 5_000
    sweep["rows"].append(_row(credit_low_notified_at="2026-09-29T10:00:00Z"))

    await cs.notify_sweep(settings=object())

    assert sweep["notify"].calls == []
    assert sweep["mark"].calls == []


async def test_the_top_up_closes_the_episode_so_the_next_one_mails(sweep):
    sweep["state"]["balance"] = 80_000
    sweep["rows"].append(_row(credit_low_notified_at="2026-09-29T10:00:00Z"))

    counts = await cs.notify_sweep(settings=object())

    assert counts["cleared"] == 1
    assert sweep["mark"].calls == [((SHOP,), {"notified": False})]
    assert sweep["notify"].calls == []


async def test_a_webapp_that_never_answered_is_retried_next_tick(sweep):
    sweep["state"]["balance"] = 5_000
    sweep["notify"].result = None
    sweep["rows"].append(_row())

    await cs.notify_sweep(settings=object())

    assert sweep["mark"].calls == []


async def test_no_owner_mailbox_still_closes_the_attempt(sweep):
    """`sent: false` is an answer: a fact about the shop, not a retry."""
    sweep["state"]["balance"] = 5_000
    sweep["notify"].result = False
    sweep["rows"].append(_row())

    await cs.notify_sweep(settings=object())

    assert sweep["mark"].calls == [((SHOP,), {"notified": True})]


async def test_a_shop_whose_responder_is_off_is_not_mailed(sweep):
    """Stamped-only rows come back so an episode can close; they are not an
    invitation to mail a shop the responder pause does not concern."""
    sweep["state"]["balance"] = 5_000
    sweep["rows"].append(_row(eligible=False))

    await cs.notify_sweep(settings=object())

    assert sweep["notify"].calls == []


async def test_one_failing_shop_does_not_abort_the_sweep(sweep, monkeypatch):
    other = uuid4()
    sweep["state"]["balance"] = 5_000
    sweep["rows"].extend([_row(), _row(shop_id=other)])
    calls = []

    async def notify(*, shop_id, **kw):
        calls.append(shop_id)
        if shop_id == SHOP:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(cs.webapp_notify, "whatsapp_credit_low", notify)
    counts = await cs.notify_sweep(settings=object())

    assert calls == [SHOP, other]
    assert counts == {"notified": 1, "cleared": 0, "errors": 1}


def test_the_candidate_query_reopens_stamped_shops_and_needs_a_live_sender():
    import inspect

    src = " ".join(inspect.getsource(cs.config_q.list_credit_notice_candidates).split())
    assert "OR c.credit_low_notified_at IS NOT NULL" in src
    assert "(c.whatsapp_agent_enabled AND s.status = 'online') AS eligible" in src
