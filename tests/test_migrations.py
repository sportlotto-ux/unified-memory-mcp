"""Crash-recovery tests for schema migrations."""

import sqlite3

import pytest

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


def test_legacy_links_rebuild_preserves_rows_and_constraints(tmp_path):
    path = tmp_path / "legacy-links.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE um_links(
            id INTEGER PRIMARY KEY,
            src_table TEXT NOT NULL,
            src_id INTEGER NOT NULL,
            dst_table TEXT NOT NULL,
            dst_id INTEGER NOT NULL,
            rel TEXT NOT NULL CHECK (rel IN
                ('supports', 'contradicts', 'supersedes', 'derives_from')),
            weight REAL NOT NULL DEFAULT 1.0,
            owner TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            valid_until REAL NOT NULL DEFAULT 0);
        INSERT INTO um_links VALUES
            (1, 'um_facts', 10, 'um_messages', 20, 'supports', 1.5,
             'alice', 's1', 1.0, 0),
            (2, 'um_edges', 3, 'um_summaries', 4, 'contradicts', 0.0,
             '', 's2', 2.0, 3.0);
        """
    )
    conn.commit()
    conn.close()

    store = Store(_cfg(path))
    try:
        assert store.conn.execute(
            "SELECT id, src_table, src_id, dst_table, dst_id, rel, weight,"
            " owner, session_id, created_at, valid_until FROM um_links"
            " ORDER BY id"
        ).fetchall() == [
            (1, "um_facts", 10, "um_messages", 20, "supports", 1.5,
             "alice", "s1", 1.0, 0.0),
            (2, "um_edges", 3, "um_summaries", 4, "contradicts", 0.0,
             "", "s2", 2.0, 3.0),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "INSERT INTO um_links(src_table, src_id, dst_table, dst_id,"
                " rel, weight, owner, session_id, created_at, valid_until)"
                " VALUES('unknown', 1, 'um_messages', 2, 'supports', 1,"
                " '', '', 1, 0)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "INSERT INTO um_links(src_table, src_id, dst_table, dst_id,"
                " rel, weight, owner, session_id, created_at, valid_until)"
                " VALUES('um_facts', 1, 'um_messages', 2, 'supports', -1,"
                " '', '', 1, 0)"
            )
    finally:
        store.close()


def test_links_rebuild_rolls_back_on_ddl_failure(tmp_path):
    path = tmp_path / "links-fault.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE um_links(
            id INTEGER PRIMARY KEY,
            src_table TEXT NOT NULL,
            src_id INTEGER NOT NULL,
            dst_table TEXT NOT NULL,
            dst_id INTEGER NOT NULL,
            rel TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            owner TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            valid_until REAL NOT NULL DEFAULT 0);
        INSERT INTO um_links VALUES
            (1, 'um_facts', 10, 'um_messages', 20, 'supports', 1.0,
             '', '', 1.0, 0);
        """
    )
    conn.commit()

    def deny_drop(action, arg1, arg2, db, source):
        if action == sqlite3.SQLITE_DROP_TABLE and arg1 == "um_links":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_drop)
    store = object.__new__(Store)
    store.conn = conn
    try:
        with pytest.raises(sqlite3.DatabaseError):
            store._migrate_links_constraints()
        assert conn.execute(
            "SELECT id, src_table FROM um_links"
        ).fetchall() == [(1, "um_facts")]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='um_links_new'"
        ).fetchone() is None
    finally:
        conn.close()


def test_orphan_link_staging_is_promoted_before_schema_create(tmp_path):
    path = tmp_path / "orphan-links-staging.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE um_links_new(
            id INTEGER PRIMARY KEY,
            src_table TEXT NOT NULL CHECK (src_table IN
                ('um_messages', 'um_facts', 'um_summaries', 'um_edges')),
            src_id INTEGER NOT NULL,
            dst_table TEXT NOT NULL CHECK (dst_table IN
                ('um_messages', 'um_facts', 'um_summaries', 'um_edges')),
            dst_id INTEGER NOT NULL,
            rel TEXT NOT NULL CHECK (rel IN
                ('supports', 'contradicts', 'supersedes', 'derives_from')),
            weight REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0),
            owner TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            valid_until REAL NOT NULL DEFAULT 0);
        INSERT INTO um_links_new VALUES
            (7, 'um_facts', 1, 'um_messages', 2, 'supports', 1.0,
             'alice', 's1', 3.0, 0);
        """
    )
    conn.commit()
    conn.close()

    store = Store(_cfg(path))
    try:
        assert store.conn.execute(
            "SELECT id, src_table, src_id, dst_table, dst_id, owner"
            " FROM um_links"
        ).fetchall() == [(7, "um_facts", 1, "um_messages", 2, "alice")]
    finally:
        store.close()


def test_owner_column_migration_is_atomic(tmp_path):
    path = tmp_path / "owner-migration.db"
    conn = sqlite3.connect(path)
    tables = ("um_messages", "um_summaries", "um_facts", "um_edges", "um_vectors")
    for table in tables:
        conn.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY)")
        conn.execute(f"INSERT INTO {table}(id) VALUES(1)")
    conn.commit()

    def deny_alter(action, arg1, arg2, db, source):
        if action == sqlite3.SQLITE_ALTER_TABLE and arg2 == "um_edges":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_alter)
    store = object.__new__(Store)
    store.conn = conn
    try:
        with pytest.raises(sqlite3.DatabaseError):
            store._migrate_owner_columns()
        for table in tables:
            cols = {row[1] for row in conn.execute(
                f"PRAGMA table_info({table})")}
            assert "owner" not in cols

        conn.set_authorizer(None)
        store._migrate_owner_columns()
        for table in tables:
            cols = {row[1] for row in conn.execute(
                f"PRAGMA table_info({table})")}
            assert "owner" in cols
    finally:
        conn.close()


def test_validity_column_migration_is_atomic(tmp_path):
    path = tmp_path / "validity-migration.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE um_facts(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
        INSERT INTO um_facts VALUES(1, 'fact');
        CREATE TABLE um_edges(id INTEGER PRIMARY KEY, predicate TEXT NOT NULL);
        INSERT INTO um_edges VALUES(1, 'edge');
        """
    )
    conn.commit()

    def deny_alter(action, arg1, arg2, db, source):
        if action == sqlite3.SQLITE_ALTER_TABLE and arg2 == "um_edges":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_alter)
    store = object.__new__(Store)
    store.conn = conn
    try:
        with pytest.raises(sqlite3.DatabaseError):
            store._migrate_validity_columns()
        fact_cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(um_facts)")}
        edge_cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(um_edges)")}
        assert "valid_until" not in fact_cols
        assert "superseded_by" not in fact_cols
        assert "valid_until" not in edge_cols

        conn.set_authorizer(None)
        store._migrate_validity_columns()
        assert conn.execute(
            "SELECT valid_until, superseded_by FROM um_facts"
        ).fetchall() == [(0.0, 0)]
        assert conn.execute(
            "SELECT valid_until FROM um_edges"
        ).fetchall() == [(0.0,)]
    finally:
        conn.close()
