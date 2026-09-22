"""Пункт 7 v0.4: hygiene/repair. Dry-run по умолчанию, ремонт backup-first."""

import os

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


def _pollute(store):
    """Сироты руками: вектор и FTS-строка без родителей."""
    store.conn.execute(
        "INSERT INTO um_vectors(owner_table, owner_id, embedding, model, owner)"
        " VALUES('um_messages', 999, X'0000', 'fake', '')")
    if store.fts:
        store.conn.execute(
            "INSERT INTO um_fts(owner_table, owner_id, body)"
            " VALUES('um_facts', 888, 'мусор')")
    store.conn.commit()


def test_hygiene_detects(ing):
    ing.remember_message("s", "user", "живая запись")
    _pollute(ing.store)
    h = ing.store.hygiene()
    assert ["um_messages", 999] in h["orphan_vectors"]
    if ing.store.fts:
        assert ["um_facts", 888] in h["orphan_fts"]
    assert h["orphan_entities"] == 0


def test_clean_dry_run_by_default(ing):
    _pollute(ing.store)
    before = ing.store.hygiene()
    assert before["orphan_vectors"]  # кандидаты есть, ничего не тронуто
    n = ing.store.select("SELECT count(*) FROM um_vectors")[0][0]
    assert n == 1  # dry-run Determined: только детект, без мутаций


def test_repair_backup_first_and_purges(ing, tmp_path):
    ing.remember_message("s", "user", "живая запись про город")
    _pollute(ing.store)
    out = ing.store.repair(dim=0)
    assert os.path.exists(out["backup"])
    assert out["backup"].startswith(str(tmp_path))
    assert out["purged_vectors"] == 1
    assert ing.store.hygiene()["orphan_vectors"] == []
    # живые данные целы и ищутся
    assert ing.router().recall("город")


def test_repair_rebuilds_fts(ing):
    ing.remember_message("s", "user", "восстановимая запись")
    if not ing.store.fts:
        pytest.skip("no FTS5 in this build")
    ing.store.conn.execute("DELETE FROM um_fts")
    ing.store.conn.commit()
    assert ing.store.fts_search("восстановимая") == []
    ing.store.repair(dim=0)
    assert ing.store.fts_search("восстановимая")


def test_repair_rebuilds_vec_index(ing):
    pytest.importorskip("sqlite_vec")
    ing.remember_message("s", "user", "векторная запись")
    out = ing.store.repair(dim=16)
    assert out["vec_index"] == 1
    assert ing.store.vec_index_status() == {"mode": "ready", "dim": 16}


def test_doctor_modes_server(tmp_path, monkeypatch):
    """mem_doctor через настоящий server-модуль: check/clean/repair + dry-run."""
    import unified_memory.server as srv
    db = tmp_path / "srv.db"
    monkeypatch.setenv("UM_DATABASE_PATH", str(db))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("UM_EMBEDDING_BACKEND", "local")
    srv._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    try:
        import json
        chk = json.loads(srv.mem_doctor(mode="check"))
        assert "hygiene" in chk and chk["integrity"] == "ok"
        dry = json.loads(srv.mem_doctor(mode="repair", apply=False))
        assert dry["apply_required"] is True and "would" in dry
        with pytest.raises(ValueError, match="unknown mode"):
            srv.mem_doctor(mode="nuke")
        rep = json.loads(srv.mem_doctor(mode="repair", apply=True))
        assert rep["backup"].startswith(str(tmp_path)) and rep["fts_rebuilt"] is True
    finally:
        srv._STATE["store"].close()
        srv._STATE.update(ingest=None, store=None, cfg=None)
