from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from booking_engine.services import number_release
from booking_engine.services.number_release import GRACE_DAYS, ReleaseInput, decide_release

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


def _settings():
    return SimpleNamespace(
        twilio_account_sid="AC1",
        twilio_auth_token="tok",
    )


# ---------------------------------------------------------------------------
# decide_release — pure policy, no DB/Twilio
# ---------------------------------------------------------------------------


def test_has_plan_nothing_scheduled_is_none():
    inp = ReleaseInput(has_plan=True, release_scheduled_at=None)
    assert decide_release(inp, NOW) == ("none", None)


def test_has_plan_something_scheduled_is_cleared():
    """Resubscribed inside the grace window — cancel the pending release."""
    scheduled = NOW + timedelta(days=3)
    inp = ReleaseInput(has_plan=True, release_scheduled_at=scheduled)
    assert decide_release(inp, NOW) == ("clear", None)


def test_no_plan_nothing_scheduled_schedules_grace_deadline():
    inp = ReleaseInput(has_plan=False, release_scheduled_at=None)
    action, deadline = decide_release(inp, NOW)
    assert action == "schedule"
    assert deadline == NOW + timedelta(days=GRACE_DAYS)


def test_no_plan_scheduled_in_future_is_none_and_keeps_deadline():
    deadline_existing = NOW + timedelta(days=5)
    inp = ReleaseInput(has_plan=False, release_scheduled_at=deadline_existing)
    assert decide_release(inp, NOW) == ("none", deadline_existing)


def test_no_plan_deadline_passed_releases():
    deadline_past = NOW - timedelta(hours=1)
    inp = ReleaseInput(has_plan=False, release_scheduled_at=deadline_past)
    assert decide_release(inp, NOW) == ("release", None)


def test_deadline_does_not_move_forward_on_repeated_ticks():
    """The bug this whole rule set exists to prevent: re-scheduling on every
    hourly tick would push the deadline forward forever and the number would
    never be released."""
    inp1 = ReleaseInput(has_plan=False, release_scheduled_at=None)
    action1, deadline1 = decide_release(inp1, NOW)
    assert action1 == "schedule"

    later = NOW + timedelta(hours=1)
    inp2 = ReleaseInput(has_plan=False, release_scheduled_at=deadline1)
    action2, deadline2 = decide_release(inp2, later)

    assert action2 == "none"
    assert deadline2 == deadline1, "the deadline must not move on a later tick"


# ---------------------------------------------------------------------------
# release_for_shop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_for_shop_no_number_row_is_a_noop(monkeypatch):
    shop_id = uuid4()

    async def fake_get_telephony(sid):
        return None

    monkeypatch.setattr(number_release.voice_telephony_queries, "get_telephony", fake_get_telephony)

    def fake_release(**kwargs):
        raise AssertionError("must not call Twilio when there is no number row")

    monkeypatch.setattr(number_release.twilio_numbers, "release_number", fake_release)

    outcome = await number_release.release_for_shop(shop_id, settings=_settings(), reason="test")
    assert outcome == "no_number"


@pytest.mark.asyncio
async def test_release_for_shop_happy_path(monkeypatch):
    shop_id = uuid4()

    async def fake_get_telephony(sid):
        return {"shop_id": sid, "kairo_number": "+37255500000", "kairo_number_sid": "PN1"}

    monkeypatch.setattr(number_release.voice_telephony_queries, "get_telephony", fake_get_telephony)

    released_kwargs = {}

    def fake_release(**kwargs):
        released_kwargs.update(kwargs)

    monkeypatch.setattr(number_release.twilio_numbers, "release_number", fake_release)

    deleted = []

    async def fake_delete_telephony(sid):
        deleted.append(sid)

    monkeypatch.setattr(number_release.voice_telephony_queries, "delete_telephony", fake_delete_telephony)

    statuses = []

    async def fake_set_status(**kwargs):
        statuses.append(kwargs)

    monkeypatch.setattr(number_release.number_request_queries, "set_status", fake_set_status)

    pushed = []

    async def fake_send_push(**kwargs):
        pushed.append(kwargs)

    monkeypatch.setattr(number_release.push_notifications, "send_push", fake_send_push)

    outcome = await number_release.release_for_shop(shop_id, settings=_settings(), reason="grace_period_expired")

    assert outcome == "released"
    assert released_kwargs == {"sid": "PN1", "account_sid": "AC1", "auth_token": "tok"}
    assert deleted == [shop_id]
    assert statuses == [{
        "shop_id": shop_id,
        "status": "released",
        "released_at_now": True,
        "released_number": "+37255500000",
    }]
    assert len(pushed) == 1
    assert pushed[0]["event"] == "number_released"


@pytest.mark.asyncio
async def test_release_for_shop_twilio_404_is_treated_as_already_released(monkeypatch):
    """The number is already gone at Twilio (e.g. a prior crashed run) — the
    local row must not outlive it, so this still proceeds to delete + record."""
    from twilio.base.exceptions import TwilioRestException

    shop_id = uuid4()

    async def fake_get_telephony(sid):
        return {"shop_id": sid, "kairo_number": "+37255500000", "kairo_number_sid": "PN1"}

    monkeypatch.setattr(number_release.voice_telephony_queries, "get_telephony", fake_get_telephony)

    def fake_release(**kwargs):
        raise TwilioRestException(status=404, uri="/x", msg="not found")

    monkeypatch.setattr(number_release.twilio_numbers, "release_number", fake_release)

    deleted = []

    async def fake_delete_telephony(sid):
        deleted.append(sid)

    monkeypatch.setattr(number_release.voice_telephony_queries, "delete_telephony", fake_delete_telephony)

    async def fake_set_status(**kwargs):
        pass

    monkeypatch.setattr(number_release.number_request_queries, "set_status", fake_set_status)

    async def fake_send_push(**kwargs):
        pass

    monkeypatch.setattr(number_release.push_notifications, "send_push", fake_send_push)

    outcome = await number_release.release_for_shop(shop_id, settings=_settings(), reason="test")

    assert outcome == "released"
    assert deleted == [shop_id]


@pytest.mark.asyncio
async def test_release_for_shop_other_twilio_error_does_not_delete_the_row(monkeypatch):
    """Any failure other than a confirmed 404 must leave the row in place so
    the next tick retries — deleting it here would leak a number we still pay
    for but no longer track."""
    from twilio.base.exceptions import TwilioRestException

    shop_id = uuid4()

    async def fake_get_telephony(sid):
        return {"shop_id": sid, "kairo_number": "+37255500000", "kairo_number_sid": "PN1"}

    monkeypatch.setattr(number_release.voice_telephony_queries, "get_telephony", fake_get_telephony)

    def fake_release(**kwargs):
        raise TwilioRestException(status=500, uri="/x", msg="server error")

    monkeypatch.setattr(number_release.twilio_numbers, "release_number", fake_release)

    def fail_delete(sid):
        raise AssertionError("must not delete the row when Twilio release failed")

    monkeypatch.setattr(number_release.voice_telephony_queries, "delete_telephony", fail_delete)

    def fail_set_status(**kwargs):
        raise AssertionError("must not mark released when Twilio release failed")

    monkeypatch.setattr(number_release.number_request_queries, "set_status", fail_set_status)

    outcome = await number_release.release_for_shop(shop_id, settings=_settings(), reason="test")
    assert outcome == "release_failed"


@pytest.mark.asyncio
async def test_release_for_shop_unexpected_exception_does_not_delete_the_row(monkeypatch):
    shop_id = uuid4()

    async def fake_get_telephony(sid):
        return {"shop_id": sid, "kairo_number": "+37255500000", "kairo_number_sid": "PN1"}

    monkeypatch.setattr(number_release.voice_telephony_queries, "get_telephony", fake_get_telephony)

    def fake_release(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(number_release.twilio_numbers, "release_number", fake_release)

    def fail_delete(sid):
        raise AssertionError("must not delete the row on an unexpected exception")

    monkeypatch.setattr(number_release.voice_telephony_queries, "delete_telephony", fail_delete)

    outcome = await number_release.release_for_shop(shop_id, settings=_settings(), reason="test")
    assert outcome == "release_failed"


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_schedules_clears_and_releases_and_counts_them(monkeypatch):
    shop_schedule = uuid4()   # no plan, nothing scheduled yet -> schedule
    shop_clear = uuid4()      # plan is back, something scheduled -> clear
    shop_release = uuid4()    # no plan, deadline passed -> release
    shop_wait = uuid4()       # no plan, deadline in the future -> none

    rows = [
        {"shop_id": shop_schedule, "kairo_number": "+1", "kairo_number_sid": "S1", "release_scheduled_at": None},
        {"shop_id": shop_clear, "kairo_number": "+2", "kairo_number_sid": "S2",
         "release_scheduled_at": NOW + timedelta(days=3)},
        {"shop_id": shop_release, "kairo_number": "+3", "kairo_number_sid": "S3",
         "release_scheduled_at": NOW - timedelta(hours=1)},
        {"shop_id": shop_wait, "kairo_number": "+4", "kairo_number_sid": "S4",
         "release_scheduled_at": NOW + timedelta(days=1)},
    ]

    async def fake_list_provisioned_numbers():
        return rows

    monkeypatch.setattr(number_release.number_request_queries, "list_provisioned_numbers", fake_list_provisioned_numbers)

    plans = {shop_schedule: False, shop_clear: True, shop_release: False, shop_wait: False}

    async def fake_has_active_plan(shop_id):
        return plans[shop_id]

    monkeypatch.setattr(number_release.number_request_queries, "has_active_plan", fake_has_active_plan)

    schedule_calls = []

    async def fake_set_release_schedule(*, shop_id, deadline):
        schedule_calls.append((shop_id, deadline))

    monkeypatch.setattr(number_release.number_request_queries, "set_release_schedule", fake_set_release_schedule)

    released_shops = []

    async def fake_release_for_shop(shop_id, *, settings, reason):
        released_shops.append(shop_id)
        return "released"

    monkeypatch.setattr(number_release, "release_for_shop", fake_release_for_shop)

    # Freeze "now" inside sweep to match the fixture's NOW.
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(number_release, "datetime", FrozenDatetime)

    counts = await number_release.sweep(settings=_settings())

    assert counts == {"scheduled": 1, "cleared": 1, "released": 1, "errors": 0}
    assert schedule_calls == [
        (shop_schedule, NOW + timedelta(days=GRACE_DAYS)),
        (shop_clear, None),
    ]
    assert released_shops == [shop_release]


@pytest.mark.asyncio
async def test_sweep_one_shop_failure_does_not_abort_the_rest(monkeypatch):
    shop_broken = uuid4()
    shop_ok = uuid4()

    rows = [
        {"shop_id": shop_broken, "kairo_number": "+1", "kairo_number_sid": "S1", "release_scheduled_at": None},
        {"shop_id": shop_ok, "kairo_number": "+2", "kairo_number_sid": "S2", "release_scheduled_at": None},
    ]

    async def fake_list_provisioned_numbers():
        return rows

    monkeypatch.setattr(number_release.number_request_queries, "list_provisioned_numbers", fake_list_provisioned_numbers)

    async def fake_has_active_plan(shop_id):
        if shop_id == shop_broken:
            raise RuntimeError("db exploded")
        return False

    monkeypatch.setattr(number_release.number_request_queries, "has_active_plan", fake_has_active_plan)

    scheduled = []

    async def fake_set_release_schedule(*, shop_id, deadline):
        scheduled.append(shop_id)

    monkeypatch.setattr(number_release.number_request_queries, "set_release_schedule", fake_set_release_schedule)

    counts = await number_release.sweep(settings=_settings())

    assert counts["errors"] == 1
    assert counts["scheduled"] == 1
    assert scheduled == [shop_ok]


@pytest.mark.asyncio
async def test_sweep_release_failure_counts_as_error(monkeypatch):
    shop_id = uuid4()
    rows = [{"shop_id": shop_id, "kairo_number": "+1", "kairo_number_sid": "S1",
             "release_scheduled_at": NOW - timedelta(hours=1)}]

    async def fake_list_provisioned_numbers():
        return rows

    monkeypatch.setattr(number_release.number_request_queries, "list_provisioned_numbers", fake_list_provisioned_numbers)

    async def fake_has_active_plan(shop_id):
        return False

    monkeypatch.setattr(number_release.number_request_queries, "has_active_plan", fake_has_active_plan)

    async def fake_release_for_shop(shop_id, *, settings, reason):
        return "release_failed"

    monkeypatch.setattr(number_release, "release_for_shop", fake_release_for_shop)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(number_release, "datetime", FrozenDatetime)

    counts = await number_release.sweep(settings=_settings())
    assert counts == {"scheduled": 0, "cleared": 0, "released": 0, "errors": 1}
