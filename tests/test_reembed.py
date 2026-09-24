"""Embedding model-change recovery tests."""

import pytest
from fake_backend import FakeBackend

from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store


class FailingBackend(FakeBackend):
    model_name = "new/failing-16"

    def __init__(self):
        self.calls = 0

    def embed_docs(self, texts):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("embedding failed")
        return super().embed_docs(texts)


def _cfg(tmp_path, name="reembed.db"):
    return Config(
        db_path=tmp_path / name,
        archive_path=tmp_path / (name + ".archive"),
        context_tokens=10**9,
        vec_index="off",
    )


def _seed(store):
    mid = store.add_message("s", "user", "message", owner="alice")
    sid = store.add_summary("s", "summary", owner="alice")
    fid = store.add_fact("c", "name", "fact", owner="alice")
    eid = store.add_edge("Alice", "likes", "tea", owner="alice")
    ent_ids = [r[0] for r in store.select(
        "SELECT id FROM um_entities WHERE owner='alice' ORDER BY id")]
    for table, oid in (("um_messages", mid), ("um_summaries", sid),
                       ("um_facts", fid), ("um_edges", eid),
                       *(("um_entities", oid) for oid in ent_ids)):
        store.add_vector(table, oid, [0.1] * 16, "old/model-16", owner="alice")


def test_reembed_replaces_all_source_vectors_and_stamp(tmp_path):
    store = Store(_cfg(tmp_path), embedding_dim=16, embedding_model="old/model-16")
    try:
        _seed(store)
        report = Ingest(store, FakeBackend(), cfg=_cfg(tmp_path)).reembed(batch=2)
        assert report["embedded"] == 6
        assert store.meta_get("embedding_model") == FakeBackend.model_name
        assert store.meta_get("embedding_dim") == "16"
        rows = store.select("SELECT model, owner FROM um_vectors")
        assert rows and all(model == FakeBackend.model_name and owner == "alice"
                            for model, owner in rows)
    finally:
        store.close()


def test_reindex_covers_summaries_and_preserves_owner(tmp_path):
    cfg = _cfg(tmp_path, "reindex.db")
    store = Store(cfg, embedding_dim=16, embedding_model=FakeBackend.model_name)
    try:
        store.add_summary("s", "summary", owner="alice")
        store.add_message("s", "user", "message", owner="alice")
        report = Ingest(store, FakeBackend(), cfg=cfg).reindex()
        assert report["embedded"] == 2
        assert set(store.select(
            "SELECT owner_table, owner, model FROM um_vectors"
        )) == {
            ("um_summaries", "alice", FakeBackend.model_name),
            ("um_messages", "alice", FakeBackend.model_name),
        }
    finally:
        store.close()


def test_reembed_failure_rolls_back_vectors_and_stamp(tmp_path):
    cfg = _cfg(tmp_path, "rollback.db")
    store = Store(cfg, embedding_dim=16, embedding_model="old/model-16")
    try:
        store.add_message("s", "user", "one", owner="alice")
        store.add_message("s", "user", "two", owner="alice")
        for oid in (1, 2):
            store.add_vector("um_messages", oid, [0.1] * 16,
                             "old/model-16", owner="alice")
        with pytest.raises(RuntimeError, match="embedding failed"):
            Ingest(store, FailingBackend(), cfg=cfg).reembed(batch=1)
        assert store.meta_get("embedding_model") == "old/model-16"
        assert store.meta_get("embedding_dim") == "16"
        assert all(row[0] == "old/model-16" for row in store.select(
            "SELECT model FROM um_vectors"))
    finally:
        store.close()


def test_reembed_cli_is_importable():
    from unified_memory.reembed import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
