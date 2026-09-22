"""Пункт 2 v0.4: user-isolation. Чужие данные не видны ни в одном туле."""

import sqlite3

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


def test_messages_isolated(ing):
    ing.remember_message("s", "user", "алиса любит чай", owner="alice")
    ing.remember_message("s", "user", "боб любит кофе", owner="bob")
    ha = ing.router().recall("любит", owner="alice")
    hb = ing.router().recall("любит", owner="bob")
    assert [h.body for h in ha] == ["алиса любит чай"]
    assert [h.body for h in hb] == ["боб любит кофе"]
    legacy = ing.router().recall("любит")  # owner="" — всё как раньше
    assert len(legacy) == 2


def test_facts_and_graph_isolated(ing):
    ing.remember_fact("pref", "drink", "алиса пьёт чай", subject="алиса",
                      predicate="пьёт", obj="чай", owner="alice")
    ing.remember_fact("pref", "drink", "боб пьёт кофе", subject="боб",
                      predicate="пьёт", obj="кофе", owner="bob")
    assert ing.store.match_entities(["алиса"], owner="alice") == ["алиса"]
    assert ing.store.match_entities(["алиса"], owner="bob") == []
    assert ing.store.match_entities(["алиса"]) == ["алиса"]
    nb = ing.store.neighbors("алиса", owner="bob")
    assert nb == []
    nb = ing.store.neighbors("алиса", owner="alice")
    assert len(nb) == 1 and nb[0]["object"] == "чай"
    # одна сущность — два тенанта: разные id, удаление чужого — False
    assert ing.store.delete_entity("алиса", owner="bob") is False
    assert ing.store.delete_entity("алиса", owner="alice") is True
    assert ing.store.neighbors("боб", owner="bob") != []


def test_expand_and_forget_guarded(ing):
    mid = ing.remember_message("s", "user", "секрет алисы", owner="alice")["id"]
    assert ing.store.get_message(mid, owner="bob") is None
    assert ing.store.get_message(mid, owner="alice")["content"] == "секрет алисы"
    assert ing.store.get_message(mid)["content"] == "секрет алисы"  # legacy видит
    fid = ing.remember_fact("k", "n", "факт алисы", owner="alice")
    assert ing.store.delete_fact(fid, owner="bob") is False
    assert ing.store.delete_fact(fid, owner="alice") is True
    eid = ing.store.add_edge("боб", "пьёт", "кофе", owner="bob")
    assert ing.store.delete_edge(eid, owner="alice") is False
    assert ing.store.delete_edge(eid, owner="bob") is True


def test_compact_assemble_scoped(ing):
    for i in range(4):
        ing.remember_message("s", "user", f"алиса заметка {i}", owner="alice")
        ing.remember_message("s", "user", f"боб заметка {i}", owner="bob")
    r = ing.compact_session("s", keep_tail=0, owner="alice")
    assert r["status"] == "compacted"
    sums = ing.store.select("SELECT owner FROM um_summaries")
    assert sums and all(s[0] == "alice" for s in sums)
    asm = ing.window.assemble("s", owner="bob")
    assert asm["summaries"] == []
    assert all("боб" in m["content"] for m in asm["tail"])
    # счётчики давления раздельные
    assert ing.store.meta_get("tokens:alice:s") is not None
    assert ing.store.meta_get("tokens:bob:s") is not None
    assert ing.store.meta_get("tokens:s") is None


def test_vectors_scoped(ing):
    ing.remember_message("s", "user", "вектор алисы", owner="alice")
    ing.remember_message("s", "user", "вектор боба", owner="bob")
    assert len(ing.store.all_vectors(owner="alice")) == 1
    assert len(ing.store.all_vectors(owner="bob")) == 1
    assert len(ing.store.all_vectors()) == 2
    hits = ing.router().recall("вектор", owner="alice")
    assert all("алисы" in h.body for h in hits)


def test_stats_owners(ing):
    ing.remember_message("s", "user", "x", owner="alice")
    ing.remember_message("s", "user", "y", owner="bob")
    ing.remember_message("s", "user", "z")
    assert ing.store.stats()["owners"] == ["", "alice", "bob"]


def test_legacy_migration(tmp_path):
    """Старая БД без owner-колонок: данные целы, видны legacy, скрыты от тенантов."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE um_messages(id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,"
                 " role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,"
                 " source TEXT NOT NULL DEFAULT 'unknown', externalized_ref TEXT)")
    conn.execute("INSERT INTO um_messages(session_id, role, content, created_at)"
                 " VALUES('s','user','старая запись', 1700000000.0)")
    conn.execute("CREATE TABLE um_entities(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
                 " display TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)")
    conn.execute("INSERT INTO um_entities(name, display, created_at)"
                 " VALUES('иван','Иван', 1700000000.0)")
    conn.execute("CREATE TABLE um_summaries(id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,"
                 " depth INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL, covers_from INTEGER,"
                 " covers_to INTEGER, superseded_by INTEGER NOT NULL DEFAULT 0,"
                 " created_at REAL NOT NULL)")
    conn.execute("CREATE TABLE um_facts(id INTEGER PRIMARY KEY, category TEXT NOT NULL,"
                 " name TEXT NOT NULL, body TEXT NOT NULL, importance REAL NOT NULL DEFAULT 0.5,"
                 " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    conn.execute("CREATE TABLE um_edges(id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL,"
                 " predicate TEXT NOT NULL, object_id INTEGER NOT NULL,"
                 " session_id TEXT NOT NULL DEFAULT '', fact_id INTEGER NOT NULL DEFAULT 0,"
                 " created_at REAL NOT NULL)")
    conn.execute("CREATE TABLE um_vectors(id INTEGER PRIMARY KEY, owner_table TEXT NOT NULL,"
                 " owner_id INTEGER NOT NULL, embedding BLOB NOT NULL, model TEXT NOT NULL)")
    conn.execute("CREATE TABLE um_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    conn.close()
    store = Store(Config(db_path=db))
    try:
        rows = store.session_messages("s")
        assert [m["content"] for m in rows] == ["старая запись"]
        assert store.session_messages("s", owner="alice") == []
        assert store.match_entities(["иван"]) == ["иван"]
        assert store.match_entities(["иван"], owner="alice") == []
        # уникальность теперь (name, owner): то же имя у тенанта — новая строка
        store.add_entity("Иван", owner="alice")
        assert store.match_entities(["иван"], owner="alice") == ["иван"]
    finally:
        store.close()
