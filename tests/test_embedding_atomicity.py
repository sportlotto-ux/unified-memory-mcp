"""Atomicity tests for ingest writes paired with embedding vectors."""

import pytest
from fake_backend import FakeBackend

from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store


def _rig(tmp_path):
    cfg = Config(db_path=tmp_path / "embedding-atomicity.db",
                 archive_path=tmp_path / "embedding-atomicity-archive.db",
                 context_tokens=10**9)
    store = Store(cfg)
    return store, Ingest(store, FakeBackend(), cfg=cfg)


def _fail_vector(store, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("embedding write failed")

    monkeypatch.setattr(store, "add_vector", fail)


def test_message_without_vector_is_rolled_back(tmp_path, monkeypatch):
    store, ingest = _rig(tmp_path)
    try:
        _fail_vector(store, monkeypatch)
        with pytest.raises(RuntimeError, match="embedding write failed"):
            ingest.remember_message("s1", "user", "message")
        assert store.select("SELECT count(*) FROM um_messages")[0][0] == 0
        assert store.select("SELECT count(*) FROM um_vectors")[0][0] == 0
    finally:
        store.close()


def test_fact_without_vector_is_rolled_back(tmp_path, monkeypatch):
    store, ingest = _rig(tmp_path)
    try:
        _fail_vector(store, monkeypatch)
        with pytest.raises(RuntimeError, match="embedding write failed"):
            ingest.remember_fact("category", "name", "body")
        assert store.select("SELECT count(*) FROM um_facts")[0][0] == 0
        assert store.select("SELECT count(*) FROM um_vectors")[0][0] == 0
    finally:
        store.close()


def test_fact_update_without_new_vector_is_rolled_back(tmp_path, monkeypatch):
    store, ingest = _rig(tmp_path)
    try:
        fid = ingest.remember_fact("category", "name", "old body")
        before_vectors = store.select("SELECT count(*) FROM um_vectors")[0][0]
        _fail_vector(store, monkeypatch)
        with pytest.raises(RuntimeError, match="embedding write failed"):
            ingest.update_fact(fid, body="new body")
        assert store.select(
            "SELECT body, valid_until FROM um_facts WHERE id=?", (fid,)
        ) == [("old body", 0.0)]
        assert store.select("SELECT count(*) FROM um_facts")[0][0] == 1
        assert store.select("SELECT count(*) FROM um_vectors")[0][0] == before_vectors
    finally:
        store.close()
