"""Crash-recovery tests for schema migrations."""

import sqlite3

from unified_memory.config import Config
from unified_memory.store import Store


def _cfg(path):
    return Config(db_path=path, context_tokens=10**9)


def test_legacy_entity_rebuild_ignores_stale_staging(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE um_entities(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            display TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
        INSERT INTO um_entities VALUES(1, 'ivan', 'Иван', 1.0);
        CREATE TABLE um_entities_new(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            display TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
            owner TEXT NOT NULL DEFAULT '', UNIQUE(name, owner));
        INSERT INTO um_entities_new VALUES(99, 'stale', 'stale', 2.0, '');
        """
    )
    conn.commit()
    conn.close()

    store = Store(_cfg(path))
    try:
        rows = store.select("SELECT name, owner FROM um_entities")
        assert rows == [("ivan", "")]
    finally:
        store.close()


def test_orphan_staging_is_promoted_before_schema_create(tmp_path):
    path = tmp_path / "orphan-staging.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE um_entities_new(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            display TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
            owner TEXT NOT NULL DEFAULT '', UNIQUE(name, owner));
        INSERT INTO um_entities_new VALUES(7, 'orphan', 'orphan', 3.0, 'alice');
        """
    )
    conn.commit()
    conn.close()

    store = Store(_cfg(path))
    try:
        assert store.select(
            "SELECT name, owner FROM um_entities") == [("orphan", "alice")]
    finally:
        store.close()


def test_entity_rebuild_rolls_back_on_ddl_failure(tmp_path):
    path = tmp_path / "fault.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE um_entities(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            display TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
        INSERT INTO um_entities VALUES(1, 'ivan', 'Иван', 1.0);
        """
    )
    conn.commit()

    def deny_drop(action, arg1, arg2, db, source):
        if action == sqlite3.SQLITE_DROP_TABLE and arg1 == "um_entities":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_drop)
    store = object.__new__(Store)
    store.conn = conn
    try:
        try:
            store._migrate_entity_owner()
        except sqlite3.DatabaseError:
            pass
        else:
            raise AssertionError("migration unexpectedly succeeded")
        assert conn.execute(
            "SELECT name FROM um_entities").fetchall() == [("ivan",)]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='um_entities_new'"
        ).fetchone() is None
    finally:
        conn.close()
