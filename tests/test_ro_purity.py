"""ro-чистота read-пути: expire_working=False отключает lazy-GC запись."""

import time

import pytest

from fake_backend import FakeBackend
from unified_memory.config import Config
from unified_memory.ingest import Ingest
from unified_memory.store import Store
from unified_memory.summarize import ExtractiveSummarizer


@pytest.fixture
def pure_ing(tmp_path):
    cfg = Config(db_path=tmp_path / "pure.db", context_tokens=1000,
                 assembly_budget=100, compact_threshold=0.5)
    store = Store(cfg)
    yield Ingest(store, FakeBackend(), ExtractiveSummarizer(), cfg)
    store.close()


def _due_fact(ing):
    fid = ing.remember_fact(
        "working", "task", "temporary deadline token", ttl_s=60)
    assert ing.store.has_vector("um_facts", fid)
    ing.store.conn.execute(
        "UPDATE um_facts SET valid_until=? WHERE id=?",
        (time.time() - 1, fid))
    ing.store.conn.commit()
    return fid


def _row(ing, fid):
    return ing.store.select(
        "SELECT valid_until, superseded_by FROM um_facts WHERE id=?",
        (fid,))[0]


def test_recall_expire_working_false_is_pure(pure_ing):
    fid = _due_fact(pure_ing)
    before = _row(pure_ing, fid)
    pure_ing.router().recall("temporary", scope="facts", expire_working=False)
    assert _row(pure_ing, fid) == before
    assert pure_ing.store.has_vector("um_facts", fid)
    # Дефолт прежний: ленивое истечение на чтении.
    pure_ing.router().recall("temporary", scope="facts")
    assert not pure_ing.store.has_vector("um_facts", fid)


def test_assemble_expire_working_false_is_pure(pure_ing):
    fid = _due_fact(pure_ing)
    before = _row(pure_ing, fid)
    pure_ing.window.assemble("s1", 100, "", include_working=True,
                             expire_working=False)
    assert _row(pure_ing, fid) == before
    assert pure_ing.store.has_vector("um_facts", fid)
    pure_ing.window.assemble("s1", 100, "", include_working=True)
    assert not pure_ing.store.has_vector("um_facts", fid)
