"""Local-time helpers. Daily limits reset on the operator's calendar day, not UTC's."""
from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from . import db


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(db.get_setting("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def now_local(now: datetime | None = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(tz())


def today(now: datetime | None = None) -> str:
    return now_local(now).date().isoformat()


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def in_window(start: str, end: str, now: datetime | None = None) -> bool:
    """Windows that cross midnight are supported (e.g. 22:00 -> 02:00)."""
    current = now_local(now).time()
    begin, finish = parse_hhmm(start), parse_hhmm(end)
    if begin <= finish:
        return begin <= current <= finish
    return current >= begin or current <= finish


def in_window_tz(start: str, end: str, tz_name: str, now: datetime | None = None) -> bool:
    """Same check, but in a specific timezone rather than the app's global one.

    Used for country send windows: AU/UK/CA/US times are set in each country's
    own local clock, not converted by hand into the operator's timezone. An
    invalid or unknown tz_name falls back to UTC rather than raising, since a
    bad country timezone should never silently block sending everywhere.
    """
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = timezone.utc
    current = base.astimezone(zone).time()
    begin, finish = parse_hhmm(start), parse_hhmm(end)
    if begin <= finish:
        return begin <= current <= finish
    return current >= begin or current <= finish
