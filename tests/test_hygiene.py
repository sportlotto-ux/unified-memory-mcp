"""v0.7.1 hygiene: удалён мёртвый холодный поиск (P3.4B) — grep-контроль + поведение."""

import pathlib
import time

import pytest

from unified_memory.config import Config
from unified_memory.store import Store

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "unified_memory"


def test_old_cold_api_name_removed():
    # P1.3 keeps the new explicit search_messages API; the old dead API name
    # archive.search must not return.
    assert "def search(" not in (SRC / "archive.py").read_text(encoding="utf-8")
    assert "include_archived" in (SRC / "server.py").read_text(encoding="utf-8")
    assert "include_archived" in (SRC / "recall.py").read_text(encoding="utf-8")


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


# ---------- P4.5: dedupe только при отсутствии индекса ----------

_COLS = ("owner, category, name, body, importance, created_at, updated_at,"
         " valid_until, superseded_by")


def test_legacy_duplicates_collapsed_then_skipped(tmp_path):
    cfg = Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                 context_tokens=10**9)
    s = Store(cfg)
    s.conn.execute("DROP INDEX ux_um_facts_live")  # симулируем legacy без индекса
    for body in ("b1", "b2"):
        s.conn.execute(
            f"INSERT INTO um_facts({_COLS}) VALUES('','c','n',?,0.5,1,1,0,0)",
            (body,))
    s.conn.commit()
    s.close()

    s2 = Store(cfg)  # первый open: dedupe схлопывает дубли
    assert s2.conn.execute(
        "SELECT count(*) FROM um_facts WHERE valid_until=0 AND name='n'"
    ).fetchone()[0] == 1
    assert s2.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='ux_um_facts_live'").fetchone()
    s2.close()

    s3 = Store(cfg)  # второй open: индекс есть → скан пропущен, живьё не тронуто
    assert s3.conn.execute(
        "SELECT count(*) FROM um_facts WHERE valid_until=0 AND name='n'"
    ).fetchone()[0] == 1
    s3.close()


def test_fresh_db_has_index(store):
    assert store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='ux_um_facts_live'").fetchone()


