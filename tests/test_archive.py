"""Пункт 3 v0.5: retention (дефолт 0) + холодный архив (вариант a2)."""

import json
import time

import pytest

from unified_memory import archive
from unified_memory.config import Config
from unified_memory.server import _maybe_maintenance
from unified_memory.store import Store

DAY = 86400


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                  context_tokens=10**9)


@pytest.fixture
def store(cfg):
    s = Store(cfg)
    yield s
    s.close()


def _fill(store, n=5, owner=""):
    ids = []
    for i in range(n):
        mid = store.add_message("s", "user", f"сообщение {i} про город", "mcp", owner)
        ids.append(mid)
    return ids


def test_config_defaults_and_validation(tmp_path):
    c = Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db")
    assert c.retention_days == 0  # дефолт: копим вечно
    assert c.archive_size_mb == 1024
    with pytest.raises(ValueError, match="UM_RETENTION_DAYS"):
        Config(db_path=tmp_path / "d.db", retention_days=-1)
    with pytest.raises(ValueError, match="UM_ARCHIVE_PATH"):
        Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "d.db")


def test_move_moves_text_and_vector(cfg, store):
    ids = _fill(store, 3)
    store.add_vector("um_messages", ids[0], [0.1] * 4, "fake/test")
    assert store.message_vector(ids[0]) is not None
    conn = archive.open_archive(cfg.archive_path)
    try:
        moved = archive.move_oldest(store, conn, limit=2,
                                    label=str(cfg.archive_path))
        assert moved == 2
        assert conn.execute("SELECT count(*) FROM ar_messages").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM ar_vectors").fetchone()[0] == 1
        got = archive.fetch_message(conn, ids[0])
        assert "сообщение 0" in got["content"]
    finally:
        conn.close()
    # горячая: заглушка + externalized_ref, вектор вынесен (a2)
    row = store.select(
        "SELECT content, externalized_ref FROM um_messages WHERE id=?", (ids[0],))[0]
    assert row[0] == "[archived]" and row[1].endswith(f"#{ids[0]}")
    assert store.message_vector(ids[0]) is None
    # третье не тронуто
    row = store.select("SELECT content FROM um_messages WHERE id=?", (ids[2],))[0]
    assert "сообщение 2" in row[0]


def test_second_move_skips_archived(cfg, store):
    _fill(store, 3)
    conn = archive.open_archive(cfg.archive_path)
    try:
        assert archive.move_oldest(store, conn, limit=2, label="x") == 2
        assert archive.move_oldest(store, conn, limit=2, label="x") == 1  # только 3-е
    finally:
        conn.close()


def test_purge_only_older_than(cfg, store):
    ids = _fill(store, 3)
    conn = archive.open_archive(cfg.archive_path)
    try:
        archive.move_oldest(store, conn, limit=3, label="x")
        # состариваем первое сообщение
        conn.execute("UPDATE ar_messages SET created_at=? WHERE id=?",
                     (time.time() - 10 * DAY, ids[0]))
        conn.commit()
        purged = archive.purge_older_than(conn, time.time() - DAY)
        assert purged == 1
        assert conn.execute("SELECT count(*) FROM ar_messages").fetchone()[0] == 2
    finally:
        conn.close()


def test_retention_zero_never_purges(cfg, store, monkeypatch):
    ids = _fill(store, 2)
    conn = archive.open_archive(cfg.archive_path)
    try:
        archive.move_oldest(store, conn, limit=2, label="x")
        conn.execute("UPDATE ar_messages SET created_at=?", (time.time() - 999 * DAY,))
        conn.commit()
    finally:
        conn.close()
    assert cfg.retention_days == 0
    _maybe_maintenance(store, cfg)  # ничего не должно удалиться
    conn = archive.open_archive(cfg.archive_path)
    try:
        assert conn.execute("SELECT count(*) FROM ar_messages").fetchone()[0] == 2
        assert store.meta_get("retention_last_run") is None
    finally:
        conn.close()


def test_maintenance_size_trigger(cfg, store, monkeypatch):
    _fill(store, 4)
    monkeypatch.setattr(store, "db_size_bytes", lambda: 2 * 1024 * 1024)
    cfg2 = Config(db_path=cfg.db_path, archive_path=cfg.archive_path,
                  context_tokens=10**9, archive_size_mb=1, archive_batch=3)
    _maybe_maintenance(store, cfg2)
    assert store.meta_get("archive_last_moved") == "3"
    assert store.meta_get("archive_last_run") is not None


def test_maintenance_weekly_stamp(cfg, store, monkeypatch):
    _fill(store, 2)
    conn = archive.open_archive(cfg.archive_path)
    try:
        archive.move_oldest(store, conn, limit=2, label="x")
        conn.execute("UPDATE ar_messages SET created_at=?", (time.time() - 30 * DAY,))
        conn.commit()
    finally:
        conn.close()
    cfg2 = Config(db_path=cfg.db_path, archive_path=cfg.archive_path,
                  context_tokens=10**9, retention_days=1, archive_size_mb=10**9)
    _maybe_maintenance(store, cfg2)  # первый проход: чистит
    conn = archive.open_archive(cfg.archive_path)
    try:
        assert conn.execute("SELECT count(*) FROM ar_messages").fetchone()[0] == 0
    finally:
        conn.close()
    stamp = store.meta_get("retention_last_run")
    assert stamp is not None
    # второй вызов не должен перезаписывать метку (в пределах 7 дней)
    _maybe_maintenance(store, cfg2)
    assert store.meta_get("retention_last_run") == stamp


def test_maintenance_error_is_contained(cfg, store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("диск полный")

    monkeypatch.setattr(archive, "open_archive", boom)
    cfg2 = Config(db_path=cfg.db_path, archive_path=cfg.archive_path,
                  context_tokens=10**9, retention_days=1)
    _maybe_maintenance(store, cfg2)  # не должно бросить
    err = store.meta_get("maintenance_error")
    assert err and "диск полный" in err


def test_server_expand_redirects_to_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("UM_DATABASE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("UM_ARCHIVE_PATH", str(tmp_path / "s.archive.db"))
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda c: None)
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    try:
        mid = json.loads(m.mem_remember(session_id="s",
                                        content="важное сообщение про бюджет"))["id"]
        moved = json.loads(m.mem_doctor(mode="archive", apply=True))["moved"]
        assert moved >= 1
        ex = json.loads(m.mem_expand(kind="message", id=mid))
        assert ex.get("archived") is True
        assert "бюджет" in ex["body"]
        st = json.loads(m.mem_status())
        assert st["archive"]["archived_messages"] >= 1
        assert st["retention_days"] == 0
        # purge при retention=0 — no-op
        assert json.loads(m.mem_doctor(mode="purge", apply=True))["skipped"]
    finally:
        if m._STATE.get("store") is not None:
            m._STATE["store"].close()
        m._STATE.update(ingest=None, store=None, cfg=None)
