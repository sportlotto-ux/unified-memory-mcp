"""P2.2: data annotations over refs (red-first).

Narrow slice: agent marks an existing ref (fact/message/summary/edge)
with kind in {useful,disputed,correction,note}. No recall-rerank change,
no background worker, no upstream annotation import.
"""

import json

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def ing(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "ann.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = Config(db_path=tmp_path / "ann.db")
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


def test_annotate_and_idempotent_noop(ing):
    fid = ing.remember_fact("long", "capital", "Paris is capital")
    first = ing.annotate("fact", fid, "useful", "confirmed by source")
    second = ing.annotate("fact", fid, "useful", "confirmed by source")
    assert first["created"] is True
    assert second == {"id": first["id"], "created": False}
    other = ing.annotate("fact", fid, "useful", "different text")
    assert other["id"] != first["id"]


def test_annotate_rejects_bad_kind_target_owner(ing):
    fid = ing.remember_fact("long", "capital", "Paris", owner="alice")
    with pytest.raises(ValueError):
        ing.annotate("fact", fid, "spam", "x")
    with pytest.raises(ValueError):
        ing.annotate("fact", 999999, "note", "ghost")
    with pytest.raises(ValueError):
        ing.annotate("fact", fid, "note", "x", owner="bob")
    with pytest.raises(ValueError):
        ing.annotate("bogus", fid, "note", "x")


def test_mem_get_exposes_annotations(srv):
    fid = json.loads(srv.mem_fact("long", "capital", "Paris"))["id"]
    before = json.loads(srv.mem_get("fact", fid))
    assert before["found"] is True and before["annotations"] == []
    aid = json.loads(srv.mem_annotate(f"fact:{fid}", "disputed", "check this"))["id"]
    after = json.loads(srv.mem_get("fact", fid))
    assert [a["id"] for a in after["annotations"]] == [aid]
    assert after["annotations"][0]["kind"] == "disputed"


def test_forget_annotation_and_cascade_on_target_delete(ing):
    fid = ing.remember_fact("long", "capital", "Paris")
    aid = ing.annotate("fact", fid, "note", "temp")["id"]
    assert ing.store.delete_annotation(aid) is True
    assert ing.store.annotations_for("um_facts", fid) == []
    aid2 = ing.annotate("fact", fid, "note", "temp2")["id"]
    assert ing.store.delete_fact(fid) is True
    assert ing.store.annotations_for("um_facts", fid) == []
    assert ing.store.delete_annotation(aid2) is False


def test_owner_isolation_and_legacy_sees_all(ing):
    fid = ing.remember_fact("long", "capital", "Paris", owner="alice")
    ing.annotate("fact", fid, "note", "alice private", owner="alice")
    assert ing.store.annotations_for("um_facts", fid, owner="bob") == []
    assert len(ing.store.annotations_for("um_facts", fid)) == 1
    assert ing.store.ref_details("um_facts", fid, owner="bob") is None


def test_annotation_value_is_redacted(ing):
    fid = ing.remember_fact("long", "capital", "Paris")
    aid = ing.annotate("fact", fid, "correction", "api_key = SECRET1234567890")["id"]
    raw = ing.store.select(
        "SELECT value FROM um_annotations WHERE id=?", (aid,))[0][0]
    assert "SECRET1234567890" not in raw
    assert "UM redaction" in raw


def test_annotations_do_not_change_recall_or_assemble(ing):
    fid = ing.remember_fact("long", "capital", "Paris is capital")
    before = [h.owner_id for h in ing.router().recall("capital", scope="facts")]
    asm_before = ing.window.assemble("s1")
    ing.annotate("fact", fid, "useful", "still paris")
    after = [h.owner_id for h in ing.router().recall("capital", scope="facts")]
    asm_after = ing.window.assemble("s1")
    assert before == after
    assert asm_before == asm_after
