"""v0.7.1 п.3 (P3.5): mem_doctor(mode=retention) — age-based проход (а)."""

import json
import os
import time
from pathlib import Path

import pytest


def _drop_archive(arc):
    for p in (Path(arc), Path(str(arc) + "-wal"), Path(str(arc) + "-shm")):
        try:
            os.remove(p)
        except OSError:
            pass


@pytest.fixture
def srv(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    arc = tmp_path / "a.archive.db"
    monkeypatch.setenv("UM_DATABASE_PATH", str(db))
    monkeypatch.setenv("UM_ARCHIVE_PATH", str(arc))
    monkeypatch.setenv("UM_RETENTION_DAYS", "7")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda c: None)
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    yield m, arc
    if m._STATE.get("store") is not None:
        m._STATE["store"].close()
    m._STATE.update(ingest=None, store=None, cfg=None)


def _msg(m, content, age_days):
    m.mem_remember(session_id="s", role="user", content=content)
    st = m._STATE["store"]
    st.conn.execute("UPDATE um_messages SET created_at=? WHERE content=?",
                    (time.time() - age_days * 86400, content))
    st.conn.commit()
    return st.conn.execute("SELECT id FROM um_messages WHERE content=?",
                           (content,)).fetchone()[0]


def test_retention_dry_run_counts_without_archive(srv):
    m, arc = srv
    _msg(m, "old", 30)
    _msg(m, "new", 0)
    _drop_archive(arc)  # изолируем doctor от ленивого maintenance
    out = json.loads(m.mem_doctor(mode="retention"))
    assert out["apply_required"] is True and out["would_move"] == 1
    assert not arc.exists()  # dry-run не создаёт архив


def test_retention_apply_moves_and_expandable(srv):
    m, arc = srv
    old = _msg(m, "old content", 30)
    _msg(m, "fresh content", 0)
    out = json.loads(m.mem_doctor(mode="retention", apply=True))
    assert out["moved"] == 1 and out["archived_messages"] == 1
    st = m._STATE["store"]
    ref = st.conn.execute("SELECT externalized_ref FROM um_messages WHERE id=?",
                          (old,)).fetchone()[0]
    assert ref  # в горячей — заглушка
    exp = json.loads(m.mem_expand(kind="message", id=old))
    assert exp["body"] == "old content" and exp["archived"] is True


def test_retention_zero_is_noop(srv, monkeypatch):
    m, _ = srv
    monkeypatch.setenv("UM_RETENTION_DAYS", "0")
    out = json.loads(m.mem_doctor(mode="retention", apply=True))
    assert out["skipped"] == "retention_days=0 (keep forever)"


def test_retention_idempotent(srv):
    m, _ = srv
    _msg(m, "old", 30)
    assert json.loads(m.mem_doctor(mode="retention", apply=True))["moved"] == 1
    assert json.loads(m.mem_doctor(mode="retention", apply=True))["moved"] == 0


def test_retention_apply_without_candidates_no_side_effects(srv):
    m, arc = srv
    _msg(m, "fresh", 0)
    _drop_archive(arc)
    out = json.loads(m.mem_doctor(mode="retention", apply=True))
    assert out["moved"] == 0
    assert not arc.exists()  # пустой прогон не создаёт архив
