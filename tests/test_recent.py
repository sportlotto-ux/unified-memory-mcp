"""Пункт 4 v0.4: mem_recent. Границы UTC как у LCM, тесты без моков времени."""

from datetime import datetime, timezone

import pytest

from unified_memory.config import Config
from unified_memory.recent import parse_period
from unified_memory.store import Store

NOW = datetime(2026, 9, 22, 15, 30, tzinfo=timezone.utc)  # вторник
DAY = 86400


def ts(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp()


def test_today_yesterday():
    w = parse_period("today", NOW)
    assert (w.start_ts, w.end_ts) == (ts(2026, 9, 22), ts(2026, 9, 23))
    w = parse_period("yesterday", NOW)
    assert (w.start_ts, w.end_ts) == (ts(2026, 9, 21), ts(2026, 9, 22))


def test_week_starts_monday_utc():
    w = parse_period("week", NOW)  # вт 22.09 -> пн 21.09 .. пн 28.09
    assert (w.start_ts, w.end_ts) == (ts(2026, 9, 21), ts(2026, 9, 28))
    w = parse_period("week", datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc))  # вс
    assert (w.start_ts, w.end_ts) == (ts(2026, 9, 14), ts(2026, 9, 21))


def test_month_nd_date_lasth():
    w = parse_period("month", NOW)
    assert (w.start_ts, w.end_ts) == (ts(2026, 9, 1), ts(2026, 10, 1))
    w = parse_period("3d", NOW)
    assert (w.start_ts, w.end_ts) == (ts(2026, 9, 20), ts(2026, 9, 23))
    w = parse_period("date:2026-01-15", NOW)
    assert (w.start_ts, w.end_ts) == (ts(2026, 1, 15), ts(2026, 1, 16))
    w = parse_period("last 2h", NOW)
    assert (w.start_ts, w.end_ts) == (NOW.timestamp() - 2 * 3600, NOW.timestamp())


def test_garbage_rejected():
    for bad in ("", "  ", "someday", "0d", "-3d", "last 0h", "date:2026-13-40"):
        with pytest.raises(ValueError):
            parse_period(bad, NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_period("today", datetime(2026, 9, 22, 15, 30))  # naive запрещён


@pytest.fixture
def store(tmp_path):
    s = Store(Config(db_path=tmp_path / "d.db"))
    yield s
    s.close()


def _msg_at(store, content, at, session="s", owner=""):
    mid = store.add_message(session, "user", content, "mcp", owner)
    store.conn.execute("UPDATE um_messages SET created_at=? WHERE id=?", (at, mid))
    store.conn.commit()
    return mid


def test_boundaries_inclusive_exclusive(store):
    w = parse_period("today", NOW)
    _msg_at(store, "ровно старт", w.start_ts)
    _msg_at(store, "середина", w.start_ts + 3600)
    _msg_at(store, "ровно конец", w.end_ts)
    _msg_at(store, "до старта", w.start_ts - 1)
    got = [r["body"] for r in store.recent(w.start_ts, w.end_ts)]
    assert got == ["середина", "ровно старт"]  # свежие first; конец исключён


def test_summaries_session_owner_filters(store):
    w = parse_period("7d", NOW)
    _msg_at(store, "алиса сегодня", NOW.timestamp() - 100, owner="alice")
    _msg_at(store, "боб сегодня", NOW.timestamp() - 100, owner="bob")
    sid = store.add_summary("s", "саммари недели", owner="alice")
    store.conn.execute("UPDATE um_summaries SET created_at=? WHERE id=?",
                       (NOW.timestamp() - 200, sid))
    store.conn.commit()
    all_rows = store.recent(w.start_ts, w.end_ts)
    assert {r["body"] for r in all_rows} == {"алиса сегодня", "боб сегодня", "саммари недели"}
    assert {r["body"] for r in store.recent(w.start_ts, w.end_ts, owner="alice")} == \
        {"алиса сегодня", "саммари недели"}
    assert [r["body"] for r in store.recent(w.start_ts, w.end_ts, session_id="nope")] == []
    assert len(store.recent(w.start_ts, w.end_ts, limit=2)) == 2
