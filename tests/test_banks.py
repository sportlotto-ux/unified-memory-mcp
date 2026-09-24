"""P2.7: banks/shared, вариант A (red-first).

Скоуп bank на фактах + um_grants (read-only). Дефолт без изменений,
legacy "" видит всё. Enforcement на границах тел.
"""

import json
import sqlite3

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "bank.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = Config(db_path=tmp_path / "bank.db")
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "server.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as module
    monkeypatch.setattr(module, "_backend", lambda cfg: None)
    module._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield module
    if module._STATE.get("store") is not None:
        module._STATE["store"].close()
    module._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)


def _fact(srv, body="family secret recipe", bank="family", owner="alice"):
    return json.loads(srv.mem_fact(
        "long", "recipe", body, owner=owner, bank=bank))["id"]


def test_bank_isolation_and_legacy(srv):
    fid = _fact(srv)
    assert json.loads(srv.mem_get("fact", fid, owner="alice"))["found"] is True
    assert json.loads(srv.mem_get("fact", fid, owner="bob"))["found"] is False
    assert json.loads(srv.mem_get("fact", fid))["found"] is True


def test_share_unshare_cycle(srv):
    fid = _fact(srv)
    first = json.loads(
        srv.mem_bank_share("family", "bob", owner="alice"))
    assert first["granted"] is True
    again = json.loads(srv.mem_bank_share("family", "bob", owner="alice"))
    assert again["granted"] is False and again["id"] == first["id"]
    assert json.loads(srv.mem_get("fact", fid, owner="bob"))["found"] is True
    hits = json.loads(srv.mem_recall("recipe", scope="facts", owner="bob"))
    assert any("family secret recipe" in h["body"] for h in hits)
    with pytest.raises(ValueError):  # грант read-only: запись чужая
        srv.mem_update(kind="fact", id=fid, body="hijacked", owner="bob")
    assert json.loads(
        srv.mem_bank_unshare("family", "bob", owner="alice"))["revoked"] is True
    assert json.loads(srv.mem_get("fact", fid, owner="bob"))["found"] is False


def test_share_rejects(srv):
    with pytest.raises(ValueError):
        srv.mem_bank_share("", "bob", owner="alice")
    with pytest.raises(ValueError):
        srv.mem_bank_share("a b", "bob", owner="alice")
    with pytest.raises(ValueError):
        srv.mem_bank_share("family", "alice", owner="alice")
    with pytest.raises(ValueError):
        srv.mem_bank_share("family", "", owner="alice")
    with pytest.raises(ValueError):
        srv.mem_fact("long", "x", "y", owner="alice", bank="a b")


def test_recall_no_leak(ing):
    ing.remember_fact("long", "recipe", "UNIQUEBANKSENTENCE zebra",
                      owner="alice", bank="family")
    hits = [h.body for h in ing.router().recall(
        "UNIQUEBANKSENTENCE", scope="facts", owner="bob")]
    assert not any("UNIQUEBANKSENTENCE" in b for b in hits)
    ing.store.share_bank("alice", "family", "bob")
    hits = [h.body for h in ing.router().recall(
        "UNIQUEBANKSENTENCE", scope="facts", owner="bob")]
    assert any("UNIQUEBANKSENTENCE" in b for b in hits)


def test_slots_independent_per_bank(ing):
    a = ing.remember_fact("long", "recipe", "one", owner="alice", bank="a")
    b = ing.remember_fact("long", "recipe", "two", owner="alice", bank="b")
    assert a != b
    c = ing.remember_fact("long", "recipe", "two", owner="alice", bank="b")
    assert c == b  # тот же слот — noop


def test_evidence_and_expand_gated(srv):
    fid = _fact(srv)
    assert json.loads(
        srv.mem_expand("fact", fid, owner="bob"))["body"] is None
    out = json.loads(srv.mem_evidence(
        "family secret recipe", [f"fact:{fid}"], "cite", owner="bob"))
    assert out["refs"] == []
    assert out["rejections"] == [
        {"kind": "um_facts", "id": fid, "reason_code": "bank_hidden"}]


def test_export_import_keeps_bank_and_grants(ing, tmp_path):
    fid = ing.remember_fact("long", "recipe", "shared cake",
                            owner="alice", bank="family")
    ing.store.share_bank("alice", "family", "bob")
    from unified_memory import export as exp
    from unified_memory import import_dump as imp
    dump = tmp_path / "banks.jsonl"
    exp.export_store(ing.store, dump)
    cfg2 = Config(db_path=tmp_path / "restore.db")
    store2 = Store(cfg2)
    try:
        imp.import_dump(store2, dump)
        assert store2.ref_details("um_facts", fid, owner="bob") is not None
        assert store2.ref_details("um_facts", fid, owner="carol") is None
    finally:
        store2.close()


def test_migration_from_old_schema(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE um_facts(id INTEGER PRIMARY KEY, owner TEXT NOT NULL,"
        " category TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,"
        " importance REAL NOT NULL DEFAULT 0.5, created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL, metadata_json TEXT NOT NULL DEFAULT '',"
        " confidence REAL NOT NULL DEFAULT 1.0, veracity TEXT NOT NULL DEFAULT '',"
        " source_ref TEXT NOT NULL DEFAULT '', valid_until REAL NOT NULL DEFAULT 0,"
        " superseded_by INTEGER NOT NULL DEFAULT 0)")
    conn.execute(
        "INSERT INTO um_facts(owner, category, name, body, created_at,"
        " updated_at) VALUES('alice','long','recipe','cake',1.0,1.0)")
    conn.execute(
        "CREATE UNIQUE INDEX ux_um_facts_live"
        " ON um_facts(owner, category, name) WHERE valid_until = 0")
    conn.commit()
    conn.close()
    store = Store(Config(db_path=path))
    try:
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(um_facts)")}
        assert "bank" in cols
        sql = store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='ux_um_facts_live'"
        ).fetchone()[0]
        assert "bank" in sql
        assert store.ref_details("um_facts", 1, owner="alice") is not None
        assert store.ref_details("um_facts", 1, owner="bob") is None
    finally:
        store.close()
