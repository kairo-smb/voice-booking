"""shop_time: Rome and London, both DST edges. Mirrors the webapp's shop-time.test.ts."""
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from booking_engine.time import shop_time as st

UTC = timezone.utc
ROME, LONDON = "Europe/Rome", "Europe/London"


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_to_shop_local_summer_and_winter():
    assert st.format_shop_local(utc(2026, 7, 1, 10, 0), ROME) == "2026-07-01 12:00"
    assert st.format_shop_local(utc(2026, 11, 20, 10, 0), ROME) == "2026-11-20 11:00"
    assert st.format_shop_local(utc(2026, 7, 1, 10, 0), LONDON) == "2026-07-01 11:00"
    assert st.format_shop_local(utc(2026, 11, 20, 10, 0), LONDON) == "2026-11-20 10:00"


def test_from_shop_local_plain():
    assert st.from_shop_local("2026-11-20 11:00", ROME) == utc(2026, 11, 20, 10, 0)
    assert st.from_shop_local("2026-07-01T12:00", ROME) == utc(2026, 7, 1, 10, 0)
    assert st.from_shop_local(datetime(2026, 7, 1, 11, 0), LONDON) == utc(2026, 7, 1, 10, 0)


def test_absolute_inputs_are_not_reinterpreted():
    assert st.from_shop_local("2026-11-20T11:00Z", ROME) == utc(2026, 11, 20, 11, 0)
    assert st.from_shop_local("2026-11-20T11:00+01:00", LONDON) == utc(2026, 11, 20, 10, 0)
    assert st.from_shop_local(utc(2026, 11, 20, 11, 0), ROME) == utc(2026, 11, 20, 11, 0)


def test_date_only_is_rejected():
    with pytest.raises(ValueError):
        st.from_shop_local("2026-11-20", ROME)


def test_spring_gap_moves_to_first_valid_minute():
    # Rome 2026-03-29 02:30 does not exist -> 03:00 CEST = 01:00Z
    assert st.from_shop_local("2026-03-29 02:30", ROME) == utc(2026, 3, 29, 1, 0)
    assert st.from_shop_local("2026-03-29 02:00", ROME) == utc(2026, 3, 29, 1, 0)
    # London 2026-03-29 01:30 does not exist -> 02:00 BST = 01:00Z
    assert st.from_shop_local("2026-03-29 01:30", LONDON) == utc(2026, 3, 29, 1, 0)
    # either side of the gap is untouched
    assert st.from_shop_local("2026-03-29 01:59", ROME) == utc(2026, 3, 29, 0, 59)
    assert st.from_shop_local("2026-03-29 03:00", ROME) == utc(2026, 3, 29, 1, 0)


def test_autumn_overlap_takes_first_occurrence():
    # Rome 2026-10-25 02:30 happens twice; first = CEST (+2) = 00:30Z
    assert st.from_shop_local("2026-10-25 02:30", ROME) == utc(2026, 10, 25, 0, 30)
    # London 2026-10-25 01:30 happens twice; first = BST (+1) = 00:30Z
    assert st.from_shop_local("2026-10-25 01:30", LONDON) == utc(2026, 10, 25, 0, 30)


def test_round_trip_outside_the_edges():
    for tz in (ROME, LONDON):
        for iso in ("2026-01-15 09:15", "2026-07-15 18:45", "2026-12-31 23:59"):
            assert st.format_shop_local(st.from_shop_local(iso, tz), tz) == iso


def test_shop_today_uses_the_shop_zone():
    now = utc(2026, 11, 14, 23, 30)
    assert st.shop_today(ROME, now) == date(2026, 11, 15)
    assert st.shop_today(LONDON, now) == date(2026, 11, 14)
    summer = utc(2026, 7, 14, 22, 30)
    assert st.shop_today(ROME, summer) == date(2026, 7, 15)
    assert st.shop_today(LONDON, summer) == date(2026, 7, 14)


def test_day_bounds_dst_lengths():
    s, e = st.shop_day_bounds(date(2026, 3, 29), ROME)
    assert (e - s).total_seconds() == 23 * 3600
    assert s == utc(2026, 3, 28, 23, 0)
    s, e = st.shop_day_bounds(date(2026, 10, 25), ROME)
    assert (e - s).total_seconds() == 25 * 3600
    s, e = st.shop_day_bounds(date(2026, 10, 25), LONDON)
    assert (e - s).total_seconds() == 25 * 3600 and s == utc(2026, 10, 24, 23, 0)
    s, e = st.shop_day_bounds(date(2026, 3, 29), LONDON)
    assert (e - s).total_seconds() == 23 * 3600 and s == utc(2026, 3, 29, 0, 0)


def test_garbage_zone_falls_back_with_a_warning(caplog):
    with caplog.at_level("WARNING"):
        assert st.safe_tz("Not/AZone").key == st.DEFAULT_TIMEZONE
        assert st.safe_tz(None).key == st.DEFAULT_TIMEZONE
    assert "shop_time" in caplog.text


async def test_shop_tz_reads_the_shop_and_caches(monkeypatch):
    calls = []

    async def fake(sql, *args):
        calls.append(args)
        return {"timezone": LONDON}

    monkeypatch.setattr("booking_engine.db.connection.execute_one", fake)
    st.clear_cache()
    sid = uuid4()
    assert (await st.shop_tz(sid)).key == LONDON
    assert (await st.shop_tz(sid)).key == LONDON
    assert len(calls) == 1  # cached


async def test_shop_tz_falls_back_when_the_lookup_fails(monkeypatch, caplog):
    async def boom(sql, *args):
        raise RuntimeError("pool down")

    monkeypatch.setattr("booking_engine.db.connection.execute_one", boom)
    st.clear_cache()
    with caplog.at_level("WARNING"):
        assert (await st.shop_tz(uuid4())).key == st.DEFAULT_TIMEZONE
    assert "lookup_failed" in caplog.text


# ---- the callers: window, drip, reminder text, in a zone that is not Rome ----

def test_send_window_follows_the_shop_zone():
    from booking_engine.services.messaging import whatsapp_send as ws
    # 08:30Z in July: 10:30 in Rome (open) but 09:30 in London (open); 07:30Z:
    # 09:30 Rome (open) vs 08:30 London (closed, 30 min to 09:00).
    now = utc(2026, 7, 1, 7, 30)
    assert ws.minutes_until_window(now, start_hour=9, end_hour=20, tz=ROME) == 0
    assert ws.minutes_until_window(now, start_hour=9, end_hour=20, tz=LONDON) == 30


def test_send_window_across_the_spring_change():
    from booking_engine.services.messaging import whatsapp_send as ws
    # London, Sat 28 Mar 21:00 GMT -> next opening Sun 29 Mar 09:00 BST = 08:00Z: 11h, not 12h.
    assert ws.minutes_until_window(utc(2026, 3, 28, 21, 0), start_hour=9, end_hour=20, tz=LONDON) == 11 * 60


def test_spread_uses_the_shop_window():
    from booking_engine.services.messaging import whatsapp_send as ws
    times = ws.spread(3, utc(2026, 7, 1, 0, 0), start_hour=9, end_hour=20, tz=LONDON)
    assert times[0] == utc(2026, 7, 1, 8, 0)  # 09:00 BST
    assert all(utc(2026, 7, 1, 8, 0) <= t < utc(2026, 7, 1, 19, 0) for t in times)


def test_reminder_text_is_the_shops_wall_clock():
    from booking_engine.services.messaging import whatsapp_automations as wa
    row = {"appointment_at": utc(2026, 11, 20, 9, 30), "first_name": "Giulia", "shop_name": "Bella"}
    assert wa.render_variables("reminder_v6", row, tz=ROME)["2"] == "venerdì 20 alle 10:30"
    assert wa.render_variables("reminder_v6", row, tz=LONDON)["2"] == "venerdì 20 alle 09:30"
    late = {**row, "appointment_at": utc(2026, 11, 20, 23, 30)}
    assert wa.render_variables("feedback_v2", late, tz=ROME)["2"] == "21 novembre"
    assert wa.render_variables("feedback_v2", late, tz=LONDON)["2"] == "20 novembre"
