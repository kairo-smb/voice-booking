"""The one place that converts between instants (UTC, what the database stores)
and the wall clock of a shop (what every human and every model reads).

    DB -> user : to_shop_local / format_shop_local / shop_today / day bounds
    user -> DB : from_shop_local / ensure_instant        (tz = the shop's)

Nothing else under `booking_engine/` may name an IANA zone, call `ZoneInfo`, or
use a bare `datetime.now()` / `date.today()` / SQL `CURRENT_DATE` for shop
logic (`tests/booking_engine/test_shop_time_fence.py` fails if it does). The
offset is read from the IANA zone FOR THAT DATE through `zoneinfo`, never a
constant delta, so DST is handled.

Same rules as the webapp (`src/lib/time/shop-time.ts`) and the engine's module,
so a proposal, a stored value and a message all agree:

  * spring gap (a local time that does not exist, 02:30 on Rome's last Sunday
    of March): moves FORWARD to the first valid minute after the gap, 02:30 ->
    03:00 (London 01:30 -> 02:00).
  * autumn overlap (a local time that happens twice): the FIRST occurrence
    (fold=0).
  * date-only values (YYYY-MM-DD: birthdays, "20 novembre", a day of the
    agenda) are never converted: `from_shop_local` rejects them.

The shop's zone is `business_app_core.shops.timezone`; `shop_tz(shop_id)` reads
it (cached for a few seconds). SQL that needs the zone reads the same column
(`AT TIME ZONE s.timezone`), never a literal.
"""
from __future__ import annotations

import logging
import re
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

UTC = timezone.utc

# The one allowed fallback, used only for a missing / unknown / garbage stored
# zone (shops.timezone is NOT NULL DEFAULT 'Europe/Rome', so this is a
# defence, not a policy). Every use warns.
DEFAULT_TIMEZONE = "Europe/Rome"

_CACHE_SECONDS = 10.0
_cache: dict[UUID, tuple[float, ZoneInfo]] = {}

TzLike = str | ZoneInfo | None


def safe_tz(tz: TzLike) -> ZoneInfo:
    """A ZoneInfo for a possibly-missing / possibly-garbage value."""
    if isinstance(tz, ZoneInfo):
        return tz
    if tz:
        try:
            return ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            logger.warning("shop_time.invalid_timezone tz=%r, using %s", tz, DEFAULT_TIMEZONE)
    else:
        logger.warning("shop_time.missing_timezone, using %s", DEFAULT_TIMEZONE)
    return ZoneInfo(DEFAULT_TIMEZONE)


async def shop_tz(shop_id: UUID) -> ZoneInfo:
    """The shop's zone, read from `shops.timezone` and cached for 10 seconds."""
    now = _time.monotonic()
    hit = _cache.get(shop_id)
    if hit and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    try:
        # Imported here so the pure helpers above import without a database.
        from booking_engine.db.connection import execute_one

        row = await execute_one(
            "SELECT timezone FROM business_app_core.shops WHERE id = $1", shop_id,
        )
        name = row["timezone"] if row else None
    except Exception:  # noqa: BLE001 — a lookup failure must not lose a message
        logger.warning("shop_time.lookup_failed shop=%s, using %s", shop_id, DEFAULT_TIMEZONE,
                       exc_info=True)
        return ZoneInfo(DEFAULT_TIMEZONE)  # not cached: retry next call
    tz = safe_tz(name)
    _cache[shop_id] = (now, tz)
    return tz


def clear_cache() -> None:
    """Test seam."""
    _cache.clear()


# ---------------------------------------------------------------- DB -> user

def to_shop_local(dt_utc: datetime, tz: TzLike) -> datetime:
    """An instant -> the same instant on the shop's wall clock (aware).

    A naive input is taken to be UTC (what the database stores).
    """
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)
    return dt_utc.astimezone(safe_tz(tz))


def format_shop_local(dt_utc: datetime, tz: TzLike) -> str:
    """`YYYY-MM-DD HH:MM` on the shop's wall clock."""
    return to_shop_local(dt_utc, tz).strftime("%Y-%m-%d %H:%M")


def shop_today(tz: TzLike, now: datetime | None = None) -> date:
    """The shop's calendar date right now."""
    return to_shop_local(now or datetime.now(UTC), tz).date()


# ---------------------------------------------------------------- user -> DB

_LOCAL_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$",
    re.IGNORECASE,
)


def is_date_only(s: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s.strip()))


def _naive_to_utc(n: datetime, zone: ZoneInfo) -> datetime:
    n = n.replace(tzinfo=None)
    first = n.replace(tzinfo=zone, fold=0)
    u = first.astimezone(UTC)
    if u.astimezone(zone).replace(tzinfo=None) == n:
        return u  # exists; in an overlap fold=0 is the first occurrence
    # In a gap: the clock jumped over n. Find the first minute that carries the
    # new offset (the transition itself), i.e. the first valid local time.
    other = n.replace(tzinfo=zone, fold=1).astimezone(UTC)
    lo, hi = sorted((u, other))  # old offset still in force at lo-ish, new at hi
    lo = lo.replace(second=0, microsecond=0)
    hi = hi.replace(second=0, microsecond=0)
    new_off = hi.astimezone(zone).utcoffset()
    while hi - lo > timedelta(minutes=1):
        mid = lo + ((hi - lo) // timedelta(minutes=2)) * timedelta(minutes=1)
        if mid.astimezone(zone).utcoffset() == new_off:
            hi = mid
        else:
            lo = mid
    return hi


def from_shop_local(value: datetime | str, tz: TzLike) -> datetime:
    """What a caller, customer or form said -> an aware UTC datetime.

    * naive datetime / `'2026-11-20 11:00'` / `'2026-11-20T11:00:30'` -> shop-local
    * aware datetime / `'...Z'` / `'...+01:00'`                       -> absolute
    Raises ValueError on anything else, date-only included: a day is not an
    instant.
    """
    zone = safe_tz(tz)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC)
        return _naive_to_utc(value, zone)
    m = _LOCAL_RE.match(value.strip())
    if not m:
        raise ValueError(f"Not a date-time: {value!r}")
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    s = int(m.group(6) or 0)
    if not (1 <= mo <= 12 and 1 <= d <= 31 and h <= 23 and mi <= 59 and s <= 59):
        raise ValueError(f"Not a date-time: {value!r}")
    if m.group(7):
        z = m.group(7).upper()
        if z == "Z":
            return datetime(y, mo, d, h, mi, s, tzinfo=UTC)
        sign = -1 if z[0] == "-" else 1
        digits = z[1:].replace(":", "")
        off = int(digits[:2]) * 60 + int(digits[2:] or 0)
        return datetime(y, mo, d, h, mi, s, tzinfo=UTC) - timedelta(minutes=sign * off)
    return _naive_to_utc(datetime(y, mo, d, h, mi, s), zone)


def ensure_instant(dt: datetime, tz: TzLike) -> datetime:
    """A request datetime -> aware UTC: naive means the shop's wall clock."""
    return from_shop_local(dt, tz)


# ----------------------------------------------------------------- day bounds

def shop_day_bounds(day: date, tz: TzLike) -> tuple[datetime, datetime]:
    """[start, end) of a shop calendar day as UTC instants (23/25h on DST days)."""
    zone = safe_tz(tz)
    start = _naive_to_utc(datetime.combine(day, time(0, 0)), zone)
    end = _naive_to_utc(datetime.combine(day + timedelta(days=1), time(0, 0)), zone)
    return start, end


def shop_today_bounds(tz: TzLike, now: datetime | None = None) -> tuple[datetime, datetime]:
    return shop_day_bounds(shop_today(tz, now), tz)


def at_shop_hour(day: date, hour: int, tz: TzLike) -> datetime:
    """The UTC instant of `hour:00` on a shop calendar day (gap -> first valid minute)."""
    return _naive_to_utc(datetime.combine(day, time(hour, 0)), safe_tz(tz))
