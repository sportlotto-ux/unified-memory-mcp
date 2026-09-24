"""v0.8 D16: mem_doctor(mode=export) — стриминговый JSONL, read-only."""

import json
from pathlib import Path

import pytest

from unified_memory.export import COMPLETE_FORMAT, export_store

CONTENT = {"um_messages", "um_summaries", "um_facts", "um_entities",
           "um_edges", "um_links", "um_vectors", "um_meta"}


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda c: None)
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield m
    if m._STATE.get("store") is not None:
        m._STATE["store"].close()
    m._STATE.update(ingest=None, store=None, cfg=None)


def _read_jsonl(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    payload = [json.loads(x) for x in lines]
    assert payload[-1] == {"format": COMPLETE_FORMAT}
    return payload[0], payload[1:-1]


def _counts(m):
    m._ingest()
    st = m._STATE["store"]
    return {t: st.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("um_facts", "um_messages", "um_links")}


def test_export_writes_jsonl_readonly(srv):
    srv.mem_fact("pref", "a", "яблоко")
    srv.mem_fact("pref", "b", "банан")
    before = _counts(srv)
    out = json.loads(srv.mem_doctor(mode="export"))
    header, rows = _read_jsonl(out["path"])
    assert header["format"] == "um-export-jsonl"
    assert header["schema_version"] == "1"
    assert out["streaming"] is True and out["archive_included"] is False
    assert out["counts"]["um_facts"] == 2
    tables = {r["table"] for r in rows}
    assert set(header["counts"]) == CONTENT      # все таблицы объявлены в header
    assert tables <= CONTENT
    assert "um_fts" not in header["counts"] and "um_vecidx" not in header["counts"]
    facts = [r["row"] for r in rows if r["table"] == "um_facts"]
    assert len(facts) == 2
    assert _counts(srv) == before  # read-only


def test_export_does_not_require_apply(srv):
    out = json.loads(srv.mem_doctor(mode="export", apply=False))
    assert out["path"].endswith(".jsonl") and out["format"] == "um-export-jsonl"


def test_export_rejects_database_path(srv):
    srv._ingest()
    store = srv._STATE["store"]
    before = Path(store._db_path).read_bytes()
    with pytest.raises(ValueError, match="database"):
        export_store(store, store._db_path)
    assert Path(store._db_path).read_bytes() == before


def test_export_rejects_symlink_to_database(srv, tmp_path):
    srv._ingest()
    store = srv._STATE["store"]
    alias = tmp_path / "alias.db"
    alias.symlink_to(store._db_path)
    before = Path(store._db_path).read_bytes()
    with pytest.raises(ValueError, match="database"):
        export_store(store, alias)
    assert Path(store._db_path).read_bytes() == before


def test_export_vectors_base64(srv, tmp_path, monkeypatch):
    """Вектора в дампе — base64 BLOB (lossless), не пересчёт."""
    import unified_memory.server as m
    from fake_backend import FakeBackend
    monkeypatch.setattr(m, "_backend", lambda c: FakeBackend())
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    srv.mem_fact("pref", "a", "яблоко")
    out = json.loads(srv.mem_doctor(mode="export"))
    _, rows = _read_jsonl(out["path"])
    vecs = [r["row"] for r in rows if r["table"] == "um_vectors"]
    assert vecs and all(isinstance(v["embedding"], str) for v in vecs)
