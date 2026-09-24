"""P2.1: bounded working-fact TTL and opt-in assembly."""

import json
import time

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


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


@pytest.fixture
def ttl_ing(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_WORKING_TTL_S", "0")
    monkeypatch.setenv("UM_WORKING_LIMIT", "2")
    cfg = Config(db_path=tmp_path / "ttl.db", context_tokens=1000,
                 assembly_budget=100, compact_threshold=0.5)
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


def test_working_ttl_is_lazy_and_preserves_expired_history(ttl_ing):
    fid = ttl_ing.remember_fact(
        "working", "task", "temporary deadline token", ttl_s=60)
    assert ttl_ing.store.has_vector("um_facts", fid)
    row = ttl_ing.store.select(
        "SELECT valid_until FROM um_facts WHERE id=?", (fid,))[0]
    assert row[0] > time.time()
    assert ttl_ing.router().recall("temporary", scope="facts")

    # Simulate the deadline without sleeping; the next read performs lazy expiry.
    ttl_ing.store.conn.execute(
        "UPDATE um_facts SET valid_until=? WHERE id=?",
        (time.time() - 1, fid))
    ttl_ing.store.conn.commit()
    assert ttl_ing.router().recall("temporary", scope="facts") == []
    assert not ttl_ing.store.has_vector("um_facts", fid)
    ttl_ing.reindex()
    assert not ttl_ing.store.has_vector("um_facts", fid)

    history = ttl_ing.router().recall(
        "temporary", scope="facts", include_expired=True)
    assert [hit.owner_id for hit in history] == [fid]


def test_working_ttl_default_config_and_explicit_zero(ttl_ing, monkeypatch):
    monkeypatch.setenv("UM_WORKING_TTL_S", "30")
    cfg = Config(db_path=ttl_ing.store._db_path, context_tokens=1000)
    ing = Ingest(ttl_ing.store, FakeBackend(), ExtractiveSummarizer(), cfg)
    fid = ing.remember_fact("working", "default", "uses config")
    assert ing.store.select(
        "SELECT valid_until FROM um_facts WHERE id=?", (fid,))[0][0] > time.time()
    off = ing.remember_fact("working", "explicit-off", "stays live", ttl_s=0)
    assert ing.store.select(
        "SELECT valid_until FROM um_facts WHERE id=?", (off,))[0][0] == 0


def test_assemble_working_is_opt_in_bounded_and_owner_scoped(ttl_ing):
    for i in range(3):
        ttl_ing.remember_fact("working", f"task-{i}", f"temporary parameters {i}",
                              ttl_s=60)
    ttl_ing.remember_fact("working", "alice", "alice private", owner="alice",
                          ttl_s=60)
    ttl_ing.remember_fact("working", "bob", "bob private", owner="bob",
                          ttl_s=60)

    legacy = ttl_ing.window.assemble("s1")
    assert "working" not in legacy
    assert all("temporary parameters" not in item["body"]
               for item in legacy["tail"])

    result = ttl_ing.window.assemble("s1", include_working=True)
    assert len(result["working"]) == 2
    assert result["tokens"] <= result["budget"]
    assert result["truncated_working"] is True

    alice = ttl_ing.window.assemble("s1", owner="alice", include_working=True)
    assert [item["name"] for item in alice["working"]] == ["alice"]
    assert ttl_ing.window.assemble(
        "s1", include_working=True)["working"]


def test_working_facts_do_not_pressure_or_trigger_summaries(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_WORKING_TTL_S", "0")
    cfg = Config(db_path=tmp_path / "pressure.db", context_tokens=100,
                 compact_threshold=0.5, assembly_budget=100)
    store = Store(cfg)
    ing = Ingest(store, FakeBackend(), None, cfg)
    try:
        ing.remember_fact("working", "task", "temporary", ttl_s=1)
        pressure = ing.window.pressure("s1")
        assert pressure.tokens_total == 0
        assert pressure.messages == 0
        assert ing.window.maybe_compact("s1")["status"] == "ok"
        assert store.select("SELECT count(*) FROM um_summaries")[0][0] == 0
    finally:
        store.close()


def test_server_mem_fact_and_mem_assemble_expose_working_options(srv):
    fid = json.loads(srv.mem_fact(
        "working", "server-task", "temporary server parameter", ttl_s=30))["id"]
    result = json.loads(srv.mem_assemble("s1", include_working=True))
    assert any(item["id"] == fid for item in result["working"])
    assert "working" not in json.loads(srv.mem_assemble("s1"))


def test_working_config_is_strict_and_nonnegative(monkeypatch, tmp_path):
    for key, value in (("UM_WORKING_TTL_S", "not-an-int"),
                       ("UM_WORKING_LIMIT", "0"),
                       ("UM_WORKING_TTL_S", "-1")):
        monkeypatch.delenv("UM_WORKING_TTL_S", raising=False)
        monkeypatch.delenv("UM_WORKING_LIMIT", raising=False)
        monkeypatch.setenv(key, value)
        with pytest.raises(ValueError, match=key):
            Config(db_path=tmp_path / f"{key}-{value}.db")
