"""v0.7: типизированные связи um_links (ADR-001). Схема + write + lifecycle + BFS."""

import sqlite3
import time

import pytest

from unified_memory.config import Config
from unified_memory.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                     context_tokens=10**9))
    yield s
    s.close()


def _link(store, src=("um_facts", 1), dst=("um_messages", 2), rel="supports",
          owner="", vu=0.0, sid=""):
    cur = store.conn.execute(
        "INSERT INTO um_links(src_table, src_id, dst_table, dst_id, rel, weight,"
        " owner, session_id, created_at, valid_until) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (src[0], src[1], dst[0], dst[1], rel, 1.0, owner, sid, time.time(), vu))
    store.conn.commit()
    return cur.lastrowid


# ---------- schema (ADR-001 D2/D3) ----------

def test_um_links_unique_live(store):
    _link(store)
    with pytest.raises(sqlite3.IntegrityError):
        _link(store)  # дубль живой запрещён
    store.conn.execute("UPDATE um_links SET valid_until=? WHERE id=1",
                       (time.time() - 1,))
    store.conn.commit()
    _link(store)  # после истечения — снова живое значение возможно


def test_um_links_rel_check(store):
    with pytest.raises(sqlite3.IntegrityError):
        _link(store, rel="mentions")  # вне словаря из 4 (ADR-001 D2)


def test_um_links_owner_in_unique(store):
    _link(store, owner="a")
    _link(store, owner="b")  # разные owner — не дубли


def test_um_links_expired_dup_allowed_by_scope(store):
    # у каждого владельца своя живая связь; истечение одной не мешает другой
    live = _link(store, owner="a")
    _link(store, owner="a", rel="contradicts")  # другой rel — отдельная связь
    store.conn.execute("DELETE FROM um_links WHERE id=?", (live,))
    store.conn.commit()
    assert store.conn.execute("SELECT count(*) FROM um_links").fetchone()[0] == 1


def test_um_links_endpoint_table_check(store):
    with pytest.raises(sqlite3.IntegrityError):
        _link(store, src=("foo", 1))
    with pytest.raises(sqlite3.IntegrityError):
        _link(store, dst=("um_wat", 1))


def test_um_links_weight_check(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO um_links(src_table, src_id, dst_table, dst_id, rel, weight,"
            " owner, session_id, created_at, valid_until)"
            " VALUES('um_facts', 1, 'um_messages', 2, 'supports', -1.0, '', '', 1.0, 0)")


# ---------- п.2 mem_link (store write-path) ----------

def _seed(store, owner=""):
    fid = store.add_fact("pref", "чай", "зелёный", owner=owner)
    mid = store.add_message("s1", "user", "люблю зелёный чай", owner=owner)
    return fid, mid


def test_link_create_and_dedup_noop(store):
    fid, mid = _seed(store)
    out = store.link("um_facts", fid, "um_messages", mid, "supports")
    assert out["created"] is True and out["id"] > 0
    assert out["src"] == f"um_facts:{fid}" and out["dst"] == f"um_messages:{mid}"
    again = store.link("um_facts", fid, "um_messages", mid, "supports")
    assert again["created"] is False and again["id"] == out["id"]  # дедуп = no-op


def test_link_bad_rel_rejected(store):
    fid, mid = _seed(store)
    with pytest.raises(ValueError, match="unknown rel"):
        store.link("um_facts", fid, "um_messages", mid, "mentions")


def test_link_dangling_endpoint_rejected(store):
    fid, _ = _seed(store)
    with pytest.raises(ValueError, match="not found"):
        store.link("um_facts", fid, "um_messages", 999, "supports")


def test_link_owner_mismatch_rejected(store):
    fid, mid = _seed(store, owner="tenant-a")
    with pytest.raises(ValueError, match="owner mismatch"):
        store.link("um_facts", fid, "um_messages", mid, "supports", owner="tenant-b")


def test_link_session_from_call_not_endpoints(store):
    fid = store.add_fact("pref", "чай", "зелёный", owner="")       # факт без сессии
    mid = store.add_message("session-X", "user", "зелёный чай", owner="")
    out = store.link("um_facts", fid, "um_messages", mid, "supports",
                     session_id="caller-sess")
    row = store.conn.execute(
        "SELECT session_id FROM um_links WHERE id=?", (out["id"],)).fetchone()
    assert row[0] == "caller-sess"


def test_link_negative_weight_rejected(store):
    fid, mid = _seed(store)
    with pytest.raises(ValueError, match="weight"):
        store.link("um_facts", fid, "um_messages", mid, "supports", weight=-1)


# ---------- п.2 mem_link (server wiring) ----------

@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "srv.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda cfg: None)  # FTS-only: без модели
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield m
    if m._STATE.get("store") is not None:
        m._STATE["store"].close()
    m._STATE.update(ingest=None, store=None, cfg=None)


def test_server_mem_link_wiring(srv):
    import json
    a = json.loads(srv.mem_fact("pref", "a", "первый"))["id"]
    b = json.loads(srv.mem_fact("pref", "b", "второй"))["id"]
    out = json.loads(srv.mem_link(f"fact:{a}", f"fact:{b}", "derives_from"))
    assert out["created"] is True and out["rel"] == "derives_from"
    assert json.loads(srv.mem_link(f"fact:{a}", f"fact:{b}", "derives_from"))["created"] is False
    with pytest.raises(ValueError):
        srv.mem_link("nope:1", f"fact:{b}", "supports")  # мусорный kind в ref
