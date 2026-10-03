"""Business-hours clock for SLA targets and the ETAs employees see.

P1 runs around the clock (a critical issue doesn't wait for Monday). Everything else counts only working
time: BUSINESS_HOURS on BUSINESS_DAYS in TIMEZONE, skipping HOLIDAYS. The dataset's pause rule is applied
by the caller: time spent waiting for the employee is added back to the target.

All inputs and outputs are timezone-aware UTC datetimes; the local timezone is used only for the calendar
and for wording ("by 3:00 pm today").
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from . import config

_DAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@lru_cache(maxsize=1)
def _calendar() -> tuple[ZoneInfo, time, time, frozenset[int], frozenset[date]]:
    tz = ZoneInfo(config.TIMEZONE)
    start_s, end_s = config.BUSINESS_HOURS.split("-")
    start, end = time.fromisoformat(start_s.strip()), time.fromisoformat(end_s.strip())
    days = set()
    for part in config.BUSINESS_DAYS.lower().split(","):
        a, _, b = part.strip().partition("-")
        lo, hi = _DAY[a[:3]], _DAY[(b or a)[:3]]
        days |= set(range(lo, hi + 1))
    holidays = set()
    if config.HOLIDAYS_FILE:
        try:
            with open(config.HOLIDAYS_FILE, encoding="utf-8") as f:
                holidays = {date.fromisoformat(line.split("#")[0].strip()) for line in f if line.split("#")[0].strip()}
        except OSError:
            pass
    return tz, start, end, frozenset(days), frozenset(holidays)


def is_24x7(priority: str | None, category_id: str | None = None) -> bool:
    return (priority or "") in config.SLA_24X7_PRIORITIES or (category_id or "") in config.SLA_24X7_CATEGORIES


def _working_day(d: date) -> bool:
    _tz, _s, _e, days, holidays = _calendar()
    return d.weekday() in days and d not in holidays


def add_hours(start: datetime, hours: float, priority: str | None, category_id: str | None = None) -> datetime:
    """Due time = start + `hours` of the right kind of time for this priority and category."""
    if is_24x7(priority, category_id):
        return start + timedelta(hours=hours)
    tz, open_t, close_t, _d, _h = _calendar()
    remaining = timedelta(hours=hours)
    cur = start.astimezone(tz)
    for _ in range(3660):  # at most ten years of days; a guard, never reached in practice
        day_open = datetime.combine(cur.date(), open_t, tz)
        day_close = datetime.combine(cur.date(), close_t, tz)
        if _working_day(cur.date()) and cur < day_close:
            cur = max(cur, day_open)
            if cur + remaining <= day_close:
                return (cur + remaining).astimezone(timezone.utc)
            remaining -= day_close - cur
        cur = datetime.combine(cur.date() + timedelta(days=1), open_t, tz)
    return (start + timedelta(hours=hours)).astimezone(timezone.utc)


def hours_between(a: datetime, b: datetime, priority: str | None, category_id: str | None = None) -> float:
    """Counted hours from a to b (working hours unless the ticket runs 24x7)."""
    if b <= a:
        return 0.0
    if is_24x7(priority, category_id):
        return (b - a).total_seconds() / 3600
    tz, open_t, close_t, _d, _h = _calendar()
    total, cur, end = 0.0, a.astimezone(tz), b.astimezone(tz)
    while cur < end:
        day_open = datetime.combine(cur.date(), open_t, tz)
        day_close = datetime.combine(cur.date(), close_t, tz)
        if _working_day(cur.date()):
            lo, hi = max(cur, day_open), min(end, day_close)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600
        cur = datetime.combine(cur.date() + timedelta(days=1), time(0), tz)
    return total


def working_hours_text() -> str:
    """'9:00 am to 6:00 pm, Monday to Friday (IST)': said when a due time falls on a later working day."""
    tz, open_t, close_t, days, _h = _calendar()
    def clock(t: time) -> str:
        return datetime.combine(date.today(), t).strftime("%I:%M %p").lstrip("0").lower()
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    d = sorted(days)
    span = (f"{names[d[0]]} to {names[d[-1]]}" if d and d == list(range(d[0], d[-1] + 1))
            else ", ".join(names[i] for i in d))
    return f"{clock(open_t)} to {clock(close_t)}, {span} ({datetime.now(tz).strftime('%Z')})"


def friendly(due: datetime, now: datetime | None = None) -> str:
    """Always names the date, so "Monday" is never ambiguous:
    'by 3:00 pm today (Sat 3 Oct)' · 'by 11:00 am tomorrow (Sun 4 Oct)' · 'by 10:00 am on Monday, 5 Oct' ·
    'by 2:00 pm on Wednesday, 14 Oct'."""
    tz = _calendar()[0]
    local, today = due.astimezone(tz), (now or datetime.now(timezone.utc)).astimezone(tz).date()
    clock = local.strftime("%I:%M %p").lstrip("0").lower()
    days = (local.date() - today).days
    short = f"{local.strftime('%a')} {local.day} {local.strftime('%b')}"
    if days <= 0:
        return f"by {clock} today ({short})"
    if days == 1:
        return f"by {clock} tomorrow ({short})"
    return f"by {clock} on {local.strftime('%A')}, {local.day} {local.strftime('%b')}"
