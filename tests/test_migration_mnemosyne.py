"""P1.6b: Mnemosyne migration adapter — durable rows, policies, atomic apply."""

import json
import sqlite3

import pytest

from unified_memory.config import Config
from unified_memory.migration import migrate_mnemosyne
from unified_memory.store import Store


def _source(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE canonical_facts(
            id INTEGER PRIMARY KEY, owner_id TEXT, category TEXT, name TEXT,
            body TEXT, source TEXT, confidence REAL, version INTEGER,
            valid_from TEXT, valid_until TEXT, created_at TEXT);
        CREATE TABLE facts(
            fact_id TEXT PRIMARY KEY, session_id TEXT, subject TEXT, predicate TEXT,
            object TEXT, timestamp TEXT, source_msg_id TEXT, confidence REAL,
            created_at TEXT);
        CREATE TABLE consolidated_facts(
            id TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
            confidence REAL, mention_count INTEGER, first_seen TEXT, last_seen TEXT,
            sources_json TEXT, veracity TEXT, superseded_by TEXT, created_at TEXT,
            updated_at TEXT);
        CREATE TABLE triples(
            id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
            valid_from TEXT, valid_until TEXT, source TEXT, confidence REAL,
            created_at TEXT);
        CREATE TABLE graph_edges(
            id INTEGER PRIMARY KEY, source TEXT, target TEXT, edge_type TEXT,
            weight REAL, timestamp TEXT, created_at TEXT);
        CREATE TABLE memoria_kg(
            id INTEGER PRIMARY KEY, session_id TEXT, subject TEXT, predicate TEXT,
            object TEXT, message_idx INTEGER, confidence REAL, source_memory_id TEXT);
        CREATE TABLE memoria_facts(
            id INTEGER PRIMARY KEY, session_id TEXT, message_idx INTEGER,
            fact_type TEXT, key TEXT, value TEXT, context_snippet TEXT,
            importance REAL, timestamp TEXT, version_id INTEGER, previous_value TEXT,
            updated_msg_idx INTEGER, valid_from_msg_idx INTEGER, valid_to_msg_idx INTEGER,
            source_memory_id TEXT);
        CREATE TABLE memories(
            id TEXT PRIMARY KEY, content TEXT, source TEXT, timestamp TEXT,
            session_id TEXT, importance REAL, metadata_json TEXT, created_at TEXT);
        CREATE TABLE working_memory(
            id TEXT PRIMARY KEY, content TEXT, source TEXT, timestamp TEXT,
            session_id TEXT, importance REAL, metadata_json TEXT, veracity TEXT,
            created_at TEXT, scope TEXT);
        CREATE TABLE episodic_memory(
            id TEXT PRIMARY KEY, content TEXT, source TEXT, timestamp TEXT,
            session_id TEXT, importance REAL, metadata_json TEXT, veracity TEXT,
            created_at TEXT, scope TEXT);
        CREATE TABLE scratchpad(
            id TEXT PRIMARY KEY, content TEXT, session_id TEXT, created_at TEXT);
        CREATE TABLE annotations(
            id INTEGER PRIMARY KEY, memory_id TEXT, kind TEXT, value TEXT,
            source TEXT, confidence REAL, created_at TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO canonical_facts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "bank-a", "profile", "drink", "tea", "chat", 0.8, 1,
             "2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z",
             "2024-01-01T00:00:00Z"),
            (2, "bank-a", "profile", "drink", "coffee", "chat", 0.9, 2,
             "2024-02-01T00:00:00Z", None, "2024-02-01T00:00:00Z"),
        ],
    )
    conn.executemany(
        "INSERT INTO triples VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (1, "alice", "likes", "tea", "2024-01-01", None, "chat", 0.8,
             "2024-01-01"),
            (2, "alice", "likes", "coffee", "2024-02-01", None, "chat", 0.9,
             "2024-02-01"),
        ],
    )
    conn.execute(
        "INSERT INTO consolidated_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("c1", "alice", "likes", "tea", 0.7, 2, "2024-01-01", "2024-02-01",
         '["triples:1"]', "reported", None, "2024-01-01", "2024-02-01"),
    )
    conn.execute(
        "INSERT INTO graph_edges VALUES(?,?,?,?,?,?,?)",
        (1, "alice", "tea", "likes", 0.6, "2024-01-01", "2024-01-01"),
    )
    conn.execute(
        "INSERT INTO memoria_kg VALUES(?,?,?,?,?,?,?,?)",
        (1, "s1", "alice", "likes", "tea", 1, 0.5, "m1"),
    )
    conn.execute(
        "INSERT INTO memoria_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "s1", 1, "preference", "language", "Python", "api_key=TOPSECRET1234", 0.7,
         "2024-01-01", 1, None, 1, 1, None, "m1"),
    )
    conn.execute(
        "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?)",
        ("m1", "durable memory", "chat", "2024-01-01", "s1", 0.6, "{}", "2024-01-01"),
    )
    conn.execute(
        "INSERT INTO working_memory VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("w1", "working item", "chat", "2024-01-01", "s1", 0.4, "{}", "stated",
         "2024-01-01", "global"),
    )
    conn.execute(
        "INSERT INTO episodic_memory VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("e1", "episode item", "chat", "2024-01-01", "s1", 0.4, "{}", "stated",
         "2024-01-01", "global"),
    )
    conn.execute(
        "INSERT INTO scratchpad VALUES(?,?,?,?)",
        ("s1", "scratch", "s1", "2024-01-01"),
    )
    conn.execute(
        "INSERT INTO annotations VALUES(?,?,?,?,?,?,?)",
        (1, "m1", "tag", "x", "chat", 0.5, "2024-01-01"),
    )
    conn.commit()
    conn.close()


def _target(tmp_path, name="target.db"):
    return Store(Config(db_path=tmp_path / name,
                        archive_path=tmp_path / (name + ".archive"),
                        context_tokens=10**9))


def test_mnemosyne_dry_run_reports_policies_without_writes(tmp_path):
    source = tmp_path / "mnemosyne.db"
    _source(source)
    target = _target(tmp_path)
    try:
        report = migrate_mnemosyne(
            target, source, dry_run=True,
            owner_map={"bank-a": "tenant-a"})
        rendered = json.dumps(report, ensure_ascii=False)
        assert report["adapter"] == "mnemosyne"
        assert report["dry_run"] is True
        assert report["planned"]["facts"] == 3
        assert report["planned"]["edges"] == 3
        assert report["planned"]["messages"] == 0
        assert report["policies"]["working_memory"] == "skip"
        assert report["policies"]["episodic_memory"] == "skip"
        assert "working_memory" in report["skipped_fields"]
        assert "episodic_memory" in report["skipped_fields"]
        assert report["reconciliation"]["counts_match"] is True
        assert "coffee" not in rendered
        assert "Python" not in rendered
        assert "TOPSECRET1234" not in rendered
        assert target.conn.execute("SELECT count(*) FROM um_facts").fetchone()[0] == 0
    finally:
        target.close()


def test_mnemosyne_apply_maps_owner_history_graph_and_metadata(tmp_path):
    source = tmp_path / "mnemosyne.db"
    _source(source)
    target = _target(tmp_path)
    try:
        report = migrate_mnemosyne(
            target, source, dry_run=False,
            owner_map={"bank-a": "tenant-a"}, default_owner="tenant-a")
        assert report["applied"] is True
        assert report["inserted"]["facts"] == 3
        assert report["inserted"]["edges"] == 3
        assert report["inserted"]["messages"] == 0
        facts = target.conn.execute(
            "SELECT owner, category, name, body, valid_until, confidence, source_ref"
            " FROM um_facts ORDER BY created_at, id"
        ).fetchall()
        assert all(row[0] == "tenant-a" for row in facts)
        canonical = [row for row in facts if ":canonical_facts:" in row[6]]
        memoria = [row for row in facts if ":memoria_facts:" in row[6]]
        assert [row[2] for row in canonical] == ["drink", "drink"]
        assert canonical[0][4] > 0 and canonical[1][4] == 0
        assert canonical[1][5] == pytest.approx(0.9)
        assert len(memoria) == 1 and memoria[0][6] == "mnemosyne:memoria_facts:1"
        metadata = target.conn.execute(
            "SELECT metadata_json FROM um_facts WHERE source_ref=?",
            ("mnemosyne:memoria_facts:1",)).fetchone()[0]
        assert "TOPSECRET1234" not in metadata
        edges = target.conn.execute(
            "SELECT e.predicate, s.name, o.name, e.confidence, e.source_ref"
            " FROM um_edges e JOIN um_entities s ON s.id=e.subject_id"
            " JOIN um_entities o ON o.id=e.object_id ORDER BY e.source_ref"
        ).fetchall()
        assert len(edges) == 3
        assert any(row[4] == "mnemosyne:triples:1" for row in edges)
        assert any(row[4] == "mnemosyne:graph_edges:1" for row in edges)
        assert report["reconciliation"]["counts_match"] is True
        assert report["recall_checks"]
        assert all(check["matched"] for check in report["recall_checks"])
    finally:
        target.close()


def test_mnemosyne_working_and_episodic_policies_are_explicit(tmp_path):
    source = tmp_path / "mnemosyne.db"
    _source(source)
    target = _target(tmp_path)
    try:
        report = migrate_mnemosyne(
            target, source, dry_run=False,
            working_policy="message", episodic_policy="message",
            memory_policy="message")
        assert report["inserted"]["messages"] == 3
        rows = target.conn.execute(
            "SELECT source_ref, source, session_id, metadata_json FROM um_messages"
            " ORDER BY source_ref"
        ).fetchall()
        assert {row[0] for row in rows} == {
            "mnemosyne:working_memory:w1",
            "mnemosyne:episodic_memory:e1",
            "mnemosyne:memories:m1",
        }
        assert all(row[2] == "s1" for row in rows)
        assert all(json.loads(row[3])["adapter"] == "mnemosyne" for row in rows)
    finally:
        target.close()


def test_mnemosyne_invalid_payload_is_atomic(tmp_path):
    source = tmp_path / "mnemosyne.db"
    _source(source)
    target = _target(tmp_path)
    try:
        with pytest.raises(ValueError, match="text exceeds"):
            migrate_mnemosyne(target, source, dry_run=False,
                              working_policy="message", max_text_chars=5)
        assert target.conn.execute("SELECT count(*) FROM um_facts").fetchone()[0] == 0
        assert target.conn.execute("SELECT count(*) FROM um_messages").fetchone()[0] == 0
    finally:
        target.close()
