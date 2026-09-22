"""Temporal выборки «что было» (v0.4-п.4, mem_recent).

Семантика границ — как у LCM `rollup_periods.parse_recent_period`:
UTC, полуинтервал [start, end), week = календарная (понедельник 00:00 UTC),
naive `now` отвергается. Подмножество периодов LCM без rollup-специфики:
today / yesterday / week / month / Nd / date:YYYY-MM-DD / last Nh.

`now` инжектится параметром — тесты детерминированы без моков времени.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

PERIODS_HELP = "today, yesterday, week, month, Nd, date:YYYY-MM-DD, last Nh"


@dataclass(frozen=True)
class PeriodWindow:
    period: str
    start_ts: float
    end_ts: float


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def parse_period(period: str, now: datetime | None = None) -> PeriodWindow:
    """Период -> UTC-окно [start, end). Мусор — громко."""
    if not isinstance(period, str) or not period.strip():
        raise ValueError("period is required")
    requested = " ".join(period.strip().lower().split())
    current = _utc_now(now)
    today = current.date()

    def win(name: str, start: datetime, end: datetime) -> PeriodWindow:
        return PeriodWindow(name, start.timestamp(), end.timestamp())

    if requested == "today":
        return win(requested, _day_start(today),
                   _day_start(today + timedelta(days=1)))
    if requested == "yesterday":
        y = today - timedelta(days=1)
        return win(requested, _day_start(y), _day_start(today))
    if requested == "week":
        monday = today - timedelta(days=today.weekday())
        return win(requested, _day_start(monday),
                   _day_start(monday + timedelta(days=7)))
    if requested == "month":
        first = today.replace(day=1)
        nxt = (first.replace(month=first.month + 1, day=1) if first.month < 12
               else first.replace(year=first.year + 1, month=1, day=1))
        return win(requested, _day_start(first), _day_start(nxt))
    m = re.fullmatch(r"date:(\d{4}-\d{2}-\d{2})", requested)
    if m:
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError as exc:
            raise ValueError("period date must be a valid YYYY-MM-DD") from exc
        return win(requested, _day_start(d), _day_start(d + timedelta(days=1)))
    m = re.fullmatch(r"(\d+)d", requested)
    if m:
        n = int(m.group(1))
        if n <= 0:
            raise ValueError("day period must be at least 1d")
        start_day = today - timedelta(days=n - 1)
        return win(requested, _day_start(start_day),
                   _day_start(today + timedelta(days=1)))
    m = re.fullmatch(r"last (\d+)h", requested)
    if m:
        h = int(m.group(1))
        if h <= 0:
            raise ValueError("hour period must be at least last 1h")
        return PeriodWindow(requested, (current - timedelta(hours=h)).timestamp(),
                            current.timestamp())
    raise ValueError(f"period must be one of: {PERIODS_HELP}")
