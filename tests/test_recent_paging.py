"""v0.8 D14: пагинация mem_recent (before_id/before_ts) — стык страниц."""

import json

import pytest

from unified_memory.config import Config
from unified_memory.store import Store


def _seed(st):
    for i in range(7):
        st.conn.execute(
            "INSERT INTO um_messages(session_id, owner, role, content, created_at,"
            " source) VALUES('s','', 'user', ?, ?, 't')", (f"m{i}", 100.0 + i))
    st.conn.commit()


@pytest.fixture
def store(tmp_path):
    st = Store(Config(db_path=tmp_path / "r.db", archive_path=tmp_path / "r.arch"))
    _seed(st)
    yield st
    st.close()


def test_store_recent_pages_contiguous_and_disjoint(store):
    p1 = store.recent(0, 10**9, limit=3)
    assert [r["body"] for r in p1] == ["m6", "m5", "m4"]
    c = p1[-1]
    p2 = store.recent(0, 10**9, limit=3, before_ts=c["created_at"], before_id=c["id"])
    assert [r["body"] for r in p2] == ["m3", "m2", "m1"]
    c2 = p2[-1]
    p3 = store.recent(0, 10**9, limit=3, before_ts=c2["created_at"], before_id=c2["id"])
    assert [r["body"] for r in p3] == ["m0"]
    ids = [r["id"] for r in p1 + p2 + p3]
    assert len(ids) == 7 and len(set(ids)) == 7        # покрыли ровно всё, без наложений


def test_tool_recent_next_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "srv.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda c: None)
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    try:
        for i in range(5):
            m.mem_remember(session_id="s", role="user", content=f"строка {i}")
        page1 = json.loads(m.mem_recent(period="today", limit=2))
        assert len(page1["items"]) == 2 and page1["next"]
        page2 = json.loads(m.mem_recent(period="today", limit=2,
                                        before_id=page1["next"]["before_id"],
                                        before_ts=page1["next"]["before_ts"]))
        page3 = json.loads(m.mem_recent(period="today", limit=2,
                                        before_id=page2["next"]["before_id"],
                                        before_ts=page2["next"]["before_ts"]))
        assert page3["next"] is None                     # хвост
        got = [it["id"] for p in (page1, page2, page3) for it in p["items"]]
        assert len(got) == 5 and len(set(got)) == 5       # contiguous + disjoint
        # страницы идут строго от свежих к старым
        order = [it["body"] for p in (page1, page2, page3) for it in p["items"]]
        assert order == [f"строка {i}" for i in range(4, -1, -1)]
    finally:
        if m._STATE.get("store") is not None:
            m._STATE["store"].close()
        m._STATE.update(ingest=None, store=None, cfg=None)
