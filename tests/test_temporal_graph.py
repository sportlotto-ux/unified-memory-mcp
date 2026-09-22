"""Пункт 1 v0.5: temporal-граф. as_of (ось валидности) vs окно создания."""

import time

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer

DAY = 86400


def _iso_ts(y, m, d):
    from datetime import date, datetime, timezone
    return datetime.combine(date(y, m, d), datetime.min.time(),
                            tzinfo=timezone.utc).timestamp()


@pytest.fixture
def ing(tmp_path):
    cfg = Config(db_path=tmp_path / "d.db", context_tokens=10**9)
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


def test_as_of_returns_historical_edge(ing):
    eid = ing.store.add_edge("Иван", "работает", "в Acme")
    old = time.time() - 10 * DAY
    ing.store.conn.execute("UPDATE um_edges SET created_at=? WHERE id=?", (old, eid))
    ing.store.conn.commit()
    ing.store.update_edge(eid, time.time() - 5 * DAY)  # истекло 5 дней назад
    # сейчас — скрыто
    assert ing.store.neighbors("иван") == []
    # на момент 7 дней назад — ребро жило
    asof = time.time() - 7 * DAY
    nbs = ing.store.neighbors("иван", as_of=asof)
    assert len(nbs) == 1 and nbs[0]["object"] == "в Acme"
    # до создания — пусто
    assert ing.store.neighbors("иван", as_of=old - DAY) == []


def test_as_of_through_router_and_server(ing):
    eid = ing.store.add_edge("Иван", "работает", "в Acme")
    old = time.time() - 10 * DAY
    ing.store.conn.execute("UPDATE um_edges SET created_at=? WHERE id=?", (old, eid))
    ing.store.conn.commit()
    ing.store.update_edge(eid, time.time() - 5 * DAY)
    live = ing.router().recall("Иван")
    assert not [h for h in live if h.owner_table == "um_edges"]
    hist = ing.router().recall("Иван", as_of=time.time() - 7 * DAY)
    assert [h for h in hist if h.owner_table == "um_edges"]


def test_expand_edge_shows_validity_window(ing):
    eid = ing.store.add_edge("Иван", "любит", "чай")
    meta = ing.store.row_meta("um_edges", eid)
    assert meta["valid_from"] > 0 and meta["valid_until"] == 0.0
    ing.store.update_edge(eid, time.time())
    meta = ing.store.row_meta("um_edges", eid)
    assert meta["valid_until"] > 0


def test_as_of_owner_isolated(ing):
    ea = ing.store.add_edge("Иван", "работает", "в Acme", owner="alice")
    ing.store.conn.execute(
        "UPDATE um_edges SET created_at=? WHERE id=?", (time.time() - DAY, ea))
    ing.store.conn.commit()
    assert ing.store.neighbors("иван", owner="bob", as_of=time.time()) == []
    assert len(ing.store.neighbors("иван", owner="alice", as_of=time.time())) == 1


def test_server_as_of_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda cfg: None)
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    try:
        m.mem_fact("p", "x", "y", subject="Иван", predicate="любит", object="чай")
        # сегодняшняя дата ловит ребро
        assert m.mem_recall(query="Иван", as_of="2100-01-01")
        # далёкое прошлое — нет
        assert m.mem_recall(query="Иван", as_of="2000-01-01") == "[]"
        with pytest.raises(ValueError, match="as_of"):
            m.mem_recall(query="Иван", as_of="open")
        with pytest.raises(ValueError, match="valid_until|as_of"):
            m.mem_recall(query="Иван", as_of="позавчера")
    finally:
        if m._STATE.get("store") is not None:
            m._STATE["store"].close()
        m._STATE.update(ingest=None, store=None, cfg=None)
