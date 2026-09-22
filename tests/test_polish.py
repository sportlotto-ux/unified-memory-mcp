"""Polish-находки ревью (P3/P4): purge dry-run без побочки, один коннект на проход."""

import json

import pytest

from unified_memory import archive
from unified_memory.config import Config
from unified_memory.server import _maybe_maintenance
from unified_memory.store import Store


def test_purge_dryrun_does_not_create_archive(tmp_path, monkeypatch):
    import unified_memory.server as m
    monkeypatch.setattr(m, "_backend", lambda c: None)
    monkeypatch.setattr(m, "_maybe_maintenance", lambda *a, **k: None)
    cfg = Config(db_path=tmp_path / "s.db", archive_path=tmp_path / "s.archive.db",
                 context_tokens=10**9, retention_days=1)
    monkeypatch.setattr(m, "load", lambda: cfg)
    m._STATE.update(ingest=None, store=None, cfg=None, backend_error=None)
    try:
        out = json.loads(m.mem_doctor(mode="purge", apply=False))
        assert out["apply_required"] is True and out["would_purge"] == 0
        assert not cfg.archive_path.exists(), "dry-run создал файл архива"
    finally:
        if m._STATE.get("store") is not None:
            m._STATE["store"].close()
        m._STATE.update(ingest=None, store=None, cfg=None)


def test_maintenance_size_uses_single_archive_conn(tmp_path, monkeypatch):
    cfg = Config(db_path=tmp_path / "d.db", archive_path=tmp_path / "a.db",
                 context_tokens=10**9, archive_size_mb=1, archive_batch=1)
    store = Store(cfg)
    try:
        for i in range(4):
            store.add_message("s", "user", f"сообщение {i}", "mcp")
        monkeypatch.setattr(store, "db_size_bytes", lambda: 2 * 1024 * 1024)
        calls = {"n": 0}
        real_open = archive.open_archive

        def counting(path):
            calls["n"] += 1
            return real_open(path)

        monkeypatch.setattr(archive, "open_archive", counting)
        _maybe_maintenance(store, cfg)
        # цикл (б) открывает архив один раз на проход, не по разу на батч
        assert calls["n"] == 1, calls["n"]
        assert store.meta_get("archive_last_moved") == "4"
    finally:
        store.close()
