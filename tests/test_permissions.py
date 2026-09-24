"""Private permissions for newly created local SQLite artifacts."""

import os
import stat

from unified_memory.archive import open_archive
from unified_memory.config import Config
from unified_memory.store import Store


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_new_store_and_archive_artifacts_are_private(tmp_path):
    old_umask = os.umask(0o022)
    store = None
    conn = None
    try:
        db = tmp_path / "private" / "memory.db"
        cfg = Config(db_path=db, archive_path=tmp_path / "private" / "archive.db")
        store = Store(cfg)
        store.add_message("s", "user", "private message")
        assert _mode(db.parent) == 0o700
        assert _mode(db) == 0o600
        for suffix in ("-wal", "-shm"):
            artifact = db.with_name(db.name + suffix)
            if artifact.exists():
                assert _mode(artifact) == 0o600

        archive_path = tmp_path / "private" / "archive.db"
        conn = open_archive(archive_path)
        assert _mode(archive_path.parent) == 0o700
        assert _mode(archive_path) == 0o600
    finally:
        if conn is not None:
            conn.close()
        if store is not None:
            store.close()
        os.umask(old_umask)


def test_existing_store_permissions_are_not_silently_rewritten(tmp_path):
    db_dir = tmp_path / "existing"
    db_dir.mkdir(mode=0o755)
    db = db_dir / "memory.db"
    cfg = Config(db_path=db, archive_path=tmp_path / "archive.db")
    store = Store(cfg)
    store.close()
    os.chmod(db, 0o644)
    os.chmod(db_dir, 0o755)

    reopened = Store(cfg)
    try:
        assert _mode(db) == 0o644
        assert _mode(db_dir) == 0o755
    finally:
        reopened.close()
