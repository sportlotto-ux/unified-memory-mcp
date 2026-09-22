"""v0.7.1 hygiene: удалён мёртвый холодный поиск (P3.4B) — grep-контроль + поведение."""

import pathlib
import time

import pytest

from unified_memory.config import Config
from unified_memory.store import Store

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "unified_memory"


def test_dead_cold_api_removed():
    # archive.search удалён целиком
    assert "def search(" not in (SRC / "archive.py").read_text(encoding="utf-8")
    # include_archived не воскрес нигде
    for p in SRC.glob("*.py"):
        assert "include_archived" not in p.read_text(encoding="utf-8"), p.name


@pytest.fixture
def store(tmp_path):
    s = Store(Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                     context_tokens=10**9))
    yield s
    s.close()


def test_archived_stub_hidden_from_context(store):
    mid = store.add_message("s", "user", "hello world")
    store.conn.execute("UPDATE um_messages SET externalized_ref=? WHERE id=?",
                       ("cold.db#1", mid))
    store.conn.commit()
    assert store.session_messages("s") == []          # контекст/компакшн
    got = store.recent(0.0, time.time() + 10, limit=10)
    assert all(i["id"] != mid for i in got)           # temporal


# ---------- P4.8: вектора вытеснённых/истёкших фактов ----------

def _add_vec(store, oid):
    store.add_vector("um_facts", oid, [0.1] * 16, "fake")


def _vlen(store, oid):
    return store.conn.execute(
        "SELECT count(*) FROM um_vectors WHERE owner_table='um_facts' AND owner_id=?",
        (oid,)).fetchone()[0]


def _fts_rows(store, oid):
    return store.conn.execute(
        "SELECT count(*) FROM um_fts WHERE owner_table='um_facts' AND owner_id=?",
        (oid,)).fetchone()[0]


def test_supersede_drops_old_vector_keeps_fts(store):
    fid = store.add_fact("c", "n", "old body")
    _add_vec(store, fid)
    out = store.update_fact(fid, body="new body")
    assert out["status"] == "superseded" and out["id"] != fid
    assert _vlen(store, fid) == 0     # вектор старой версии удалён
    assert _fts_rows(store, fid) == 1  # текст старой версии цел


def test_expire_drops_vector(store):
    fid = store.add_fact("c", "n", "body")
    _add_vec(store, fid)
    store.update_fact(fid, valid_until=time.time())
    assert _vlen(store, fid) == 0
    vu = store.conn.execute(
        "SELECT valid_until FROM um_facts WHERE id=?", (fid,)).fetchone()[0]
    assert vu > 0


def test_old_version_text_survives_for_include_expired(store):
    fid = store.add_fact("c", "n", "уникальноеслово")
    store.update_fact(fid, body="другое тело")
    hits = store.fts_search("уникальноеслово", scope="facts", include_expired=True,
                            limit=5)
    assert ("um_facts", fid) in {(h.owner_table, h.owner_id) for h in hits}


def test_reopen_without_vector_until_reindex(store):
    fid = store.add_fact("c", "n", "body")
    _add_vec(store, fid)
    store.update_fact(fid, valid_until=time.time())   # expire → вектор убран
    out = store.update_fact(fid, valid_until=0.0)      # reopen
    assert out["status"] == "reopened"
    assert _vlen(store, fid) == 0                      # до mem_reindex

