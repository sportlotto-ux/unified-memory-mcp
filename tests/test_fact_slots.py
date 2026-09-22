"""Пункт 0 v0.5: valid_until + слот-семантика фактов + mem_update (красные → зелёные)."""

import sqlite3
import time

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path):
    cfg = Config(db_path=tmp_path / "d.db", context_tokens=10**9)
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


# --- слот-семантика add_fact ---------------------------------------------

def test_same_body_noop(ing):
    a = ing.store.add_fact("preference", "чай", "любит зелёный")
    b = ing.store.add_fact("preference", "чай", "любит зелёный")
    assert a == b
    live = ing.store.select(
        "SELECT count(*) FROM um_facts WHERE valid_until=0")[0][0]
    assert live == 1


def test_new_body_supersedes(ing):
    old = ing.store.add_fact("preference", "чай", "любит зелёный")
    new = ing.store.add_fact("preference", "чай", "любит чёрный")
    assert new != old
    row = ing.store.select(
        "SELECT valid_until, superseded_by FROM um_facts WHERE id=?", (old,))[0]
    assert row[0] > 0 and row[1] == new
    assert [r[0] for r in ing.store.select(
        "SELECT id FROM um_facts WHERE valid_until=0")] == [new]


def test_history_via_include_expired(ing):
    ing.store.add_fact("preference", "чай", "любит зелёный")
    ing.store.add_fact("preference", "чай", "любит чёрный")
    live = ing.router().recall("чай", scope="facts")
    assert [h.body for h in live] == ["чай: любит чёрный"]
    hist = ing.router().recall("чай", scope="facts", include_expired=True)
    assert {h.body for h in hist} == {"чай: любит чёрный", "чай: любит зелёный"}


def test_slot_unique_is_structural(ing):
    ing.store.add_fact("preference", "чай", "а")
    with pytest.raises(sqlite3.IntegrityError):
        ing.store.conn.execute(
            "INSERT INTO um_facts(owner, category, name, body, importance,"
            " created_at, updated_at, valid_until, superseded_by)"
            " VALUES('','preference','чай','б',0.5,1,1,0,0)")
    ing.store.conn.rollback()


def test_same_slot_different_owner_ok(ing):
    a = ing.store.add_fact("preference", "чай", "алиса: зелёный", owner="alice")
    b = ing.store.add_fact("preference", "чай", "боб: чёрный", owner="bob")
    assert a != b
    assert ing.store.select(
        "SELECT count(*) FROM um_facts WHERE valid_until=0")[0][0] == 2


# --- mem_update ----------------------------------------------------------

def test_update_body_creates_version(ing):
    fid = ing.store.add_fact("preference", "чай", "зелёный")
    out = ing.store.update_fact(fid, body="чёрный")
    assert out["status"] == "superseded" and out["id"] != fid
    assert ing.store.select(
        "SELECT superseded_by FROM um_facts WHERE id=?", (fid,))[0][0] == out["id"]
    assert [r[0] for r in ing.store.select(
        "SELECT body FROM um_facts WHERE valid_until=0")] == ["чёрный"]


def test_update_expire_hides_and_reopen(ing):
    fid = ing.store.add_fact("preference", "чай", "зелёный")
    assert ing.store.update_fact(fid, valid_until=time.time())["status"] == "expired"
    assert ing.router().recall("чай", scope="facts") == []
    assert len(ing.router().recall("чай", scope="facts", include_expired=True)) == 1
    assert ing.store.update_fact(fid, valid_until=0)["status"] == "reopened"
    assert len(ing.router().recall("чай", scope="facts")) == 1


def test_update_importance_in_place(ing):
    fid = ing.store.add_fact("preference", "чай", "зелёный", importance=0.2)
    out = ing.store.update_fact(fid, importance=0.9)
    assert out["id"] == fid and out["status"] == "updated"
    assert ing.store.select(
        "SELECT importance FROM um_facts WHERE id=?", (fid,))[0][0] == 0.9


def test_update_owner_guard(ing):
    fid = ing.store.add_fact("preference", "чай", "зелёный", owner="alice")
    assert ing.store.update_fact(fid, body="чёрный", owner="bob") is None
    assert ing.store.select(
        "SELECT body FROM um_facts WHERE valid_until=0")[0][0] == "зелёный"


def test_update_edge_expire_hides(ing):
    eid = ing.store.add_edge("Иван", "любит", "чай")
    assert len(ing.store.neighbors("иван")) == 1
    assert ing.store.update_edge(eid, time.time()) is True
    assert ing.store.neighbors("иван") == []
    assert len(ing.store.neighbors("иван", include_expired=True)) == 1


# --- миграция ------------------------------------------------------------

def test_legacy_dedupe_migration(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE um_facts(id INTEGER PRIMARY KEY, category TEXT NOT NULL,"
                 " name TEXT NOT NULL, body TEXT NOT NULL, importance REAL NOT NULL DEFAULT 0.5,"
                 " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    conn.execute("INSERT INTO um_facts(category,name,body,importance,created_at,updated_at)"
                 " VALUES('preference','чай','зелёный',0.5,1,1)")
    conn.execute("INSERT INTO um_facts(category,name,body,importance,created_at,updated_at)"
                 " VALUES('preference','чай','чёрный',0.5,2,2)")
    conn.execute("CREATE TABLE um_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    conn.close()
    store = Store(Config(db_path=db))
    try:
        live = store.select("SELECT body FROM um_facts WHERE valid_until=0")
        assert [r[0] for r in live] == ["чёрный"]  # новейший (max id) выжил
        assert store.select("SELECT count(*) FROM um_facts")[0][0] == 2  # lossless
    finally:
        store.close()


def test_legacy_facts_stay_live(tmp_path):
    db = tmp_path / "legacy2.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE um_facts(id INTEGER PRIMARY KEY, category TEXT NOT NULL,"
                 " name TEXT NOT NULL, body TEXT NOT NULL, importance REAL NOT NULL DEFAULT 0.5,"
                 " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    conn.execute("INSERT INTO um_facts(category,name,body,importance,created_at,updated_at)"
                 " VALUES('c','n','живой факт',0.5,1,1)")
    conn.execute("CREATE TABLE um_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    conn.close()
    store = Store(Config(db_path=db))
    try:
        assert store.select("SELECT count(*) FROM um_facts WHERE valid_until=0")[0][0] == 1
    finally:
        store.close()
