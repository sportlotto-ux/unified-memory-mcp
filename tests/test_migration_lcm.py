"""P1.6a: LCM migration adapter — dry-run first, reconciliation, atomic apply."""

import json
import sqlite3

import pytest

from unified_memory.config import Config
from unified_memory.export import export_store
from unified_memory.import_dump import import_dump
from unified_memory.migration import migrate_lcm
from unified_memory.store import Store


def _source(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages(
            store_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            source TEXT,
            conversation_id TEXT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL,
            token_estimate INTEGER,
            pinned INTEGER,
            ingested_at REAL,
            observed_at REAL,
            observed_at_source TEXT
        );
        CREATE TABLE summary_nodes(
            node_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            depth INTEGER,
            summary TEXT NOT NULL,
            token_count INTEGER,
            source_token_count INTEGER,
            source_ids TEXT,
            source_type TEXT,
            created_at REAL,
            earliest_at REAL,
            latest_at REAL,
            expand_hint TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (20, "s1", "chat", "conv-1", "user", "первое сообщение",
             None, None, None, 20.0, 4, 0, 21.0, 20.0, "event"),
            (10, "s1", "chat", "conv-1", "assistant", "второе сообщение",
             "call-1", '{"name":"search","api_key":"TOPSECRET1234"}', "search", 10.0,
             5, 0, 11.0, 10.0, "event"),
        ],
    )
    conn.execute(
        "INSERT INTO summary_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "s1", 0, "сводка", 2, 2, "[10,20]", "messages", 30.0,
         10.0, 20.0, "open"),
    )
    conn.commit()
    conn.close()


def _target(tmp_path, name="target.db"):
    return Store(Config(db_path=tmp_path / name,
                        archive_path=tmp_path / (name + ".archive"),
                        context_tokens=10**9))


def test_lcm_dry_run_is_read_only_and_has_reconciliation(tmp_path):
    source = tmp_path / "lcm.db"
    _source(source)
    target = _target(tmp_path)
    try:
        before = target.conn.execute("SELECT count(*) FROM um_messages").fetchone()[0]
        report = migrate_lcm(target, source, dry_run=True)
        rendered = json.dumps(report, ensure_ascii=False)
        assert report["adapter"] == "lcm"
        assert report["dry_run"] is True
        assert report["source_counts"] == {"messages": 2, "summary_nodes": 1}
        assert report["planned"]["messages"] == 2
        assert report["planned"]["summaries"] == 0
        assert "summary_nodes" in report["skipped_fields"]
        assert target.conn.execute("SELECT count(*) FROM um_messages").fetchone()[0] == before
        assert "первое сообщение" not in rendered
        assert "второе сообщение" not in rendered
        assert "TOPSECRET1234" not in rendered
        assert report["reconciliation"]["counts_match"] is True
    finally:
        target.close()


def test_lcm_apply_preserves_order_conversation_and_tool_metadata(tmp_path):
    source = tmp_path / "lcm.db"
    _source(source)
    target = _target(tmp_path)
    try:
        report = migrate_lcm(target, source, dry_run=False)
        assert report["applied"] is True
        assert report["inserted"]["messages"] == 2
        rows = target.conn.execute(
            "SELECT id, session_id, source, conversation_id, source_order,"
            " source_ref, metadata_json, content FROM um_messages ORDER BY source_order"
        ).fetchall()
        assert [row[5] for row in rows] == [
            "lcm:messages:10", "lcm:messages:20"
        ]
        assert all(row[2] == "chat" and row[3] == "conv-1" for row in rows)
        metadata = json.loads(rows[0][6])
        assert metadata["tool_name"] == "search"
        assert metadata["tool_call_id"] == "call-1"
        assert "TOPSECRET1234" not in rows[0][6]
        assert rows[0][7] == "второе сообщение"
        assert report["reconciliation"]["counts_match"] is True
        assert report["recall_checks"]
        assert all(check["matched"] for check in report["recall_checks"])
    finally:
        target.close()


def test_lcm_preserve_summaries_requires_explicit_strategy(tmp_path):
    source = tmp_path / "lcm.db"
    _source(source)
    target = _target(tmp_path)
    try:
        report = migrate_lcm(target, source, dry_run=False,
                             summary_strategy="preserve")
        assert report["inserted"]["summaries"] == 1
        summary_id, body = target.conn.execute(
            "SELECT id, body FROM um_summaries"
        ).fetchone()
        assert body == "сводка"
        assert target.conn.execute(
            "SELECT count(*) FROM um_summary_sources WHERE summary_id=?",
            (summary_id,),
        ).fetchone()[0] == 2
    finally:
        target.close()


def test_lcm_metadata_survives_um_export_import(tmp_path):
    source = tmp_path / "lcm.db"
    _source(source)
    target = _target(tmp_path, "migration.db")
    exported = _target(tmp_path, "roundtrip.db")
    try:
        migrate_lcm(target, source, dry_run=False)
        dump = tmp_path / "migration.jsonl"
        export_store(target, dump)
        import_dump(exported, dump)
        row = exported.conn.execute(
            "SELECT conversation_id, source_order, source_ref, metadata_json"
            " FROM um_messages ORDER BY source_order LIMIT 1"
        ).fetchone()
        assert row[0] == "conv-1"
        assert row[1] == 10
        assert row[2] == "lcm:messages:10"
        assert json.loads(row[3])["tool_name"] == "search"
    finally:
        exported.close()
        target.close()


def test_lcm_apply_rolls_back_on_invalid_source(tmp_path):
    source = tmp_path / "lcm.db"
    _source(source)
    target = _target(tmp_path)
    try:
        # Make one source row exceed the target input cap; the transaction must
        # not leave the earlier valid row behind.
        target.conn.execute(
            "INSERT INTO um_messages(session_id, role, content, created_at)"
            " VALUES(?,?,?,?)",
            ("existing", "user", "existing", 1.0),
        )
        target.conn.commit()
        with pytest.raises(ValueError, match="text exceeds"):
            migrate_lcm(target, source, dry_run=False,
                        max_text_chars=10)
        assert target.conn.execute(
            "SELECT count(*) FROM um_messages WHERE session_id='s1'"
        ).fetchone()[0] == 0
    finally:
        target.close()
