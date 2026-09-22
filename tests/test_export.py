"""v0.7 п.7: mem_doctor(mode=export) — JSON-файл, read-only, schema_version."""

import json
from pathlib import Path

import pytest


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


def _counts(m):
    m._ingest()
    st = m._STATE["store"]
    return {t: st.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("um_facts", "um_messages", "um_links")}


def test_export_writes_valid_json_readonly(srv):
    srv.mem_fact("pref", "a", "яблоко")
    srv.mem_fact("pref", "b", "банан")
    before = _counts(srv)
    out = json.loads(srv.mem_doctor(mode="export"))
    p = Path(out["path"])
    assert p.exists() and out["bytes"] > 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema_version"] == "1"
    assert {"um_facts", "um_messages", "um_edges", "um_links",
            "um_vectors"} <= set(data["tables"])
    assert out["counts"]["um_facts"] == 2
    assert len(data["tables"]["um_facts"]) == 2
    assert out["archive_included"] is False
    assert _counts(srv) == before  # read-only


def test_export_does_not_require_apply(srv):
    out = json.loads(srv.mem_doctor(mode="export", apply=False))
    assert "path" in out and "schema_version" in out
