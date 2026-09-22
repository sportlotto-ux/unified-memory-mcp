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
