"""P1.6 migration adapters.

The adapters consume upstream SQLite snapshots read-only.  They never open the
source with a write-capable connection and they never copy source payloads into
reports.  The first adapter is LCM; Mnemosyne is intentionally a separate
adapter because its durable rows and policies are different.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, load
from .redact import redact_text
from .store import Store, estimate_tokens, tokenize


_LCM_MESSAGE_COLUMNS = (
    "store_id", "session_id", "source", "conversation_id", "role", "content",
    "tool_call_id", "tool_calls", "tool_name", "timestamp", "token_estimate",
    "pinned", "ingested_at", "observed_at", "observed_at_source",
)
_LCM_SUMMARY_COLUMNS = (
    "node_id", "session_id", "depth", "summary", "token_count",
    "source_token_count", "source_ids", "source_type", "created_at",
    "earliest_at", "latest_at", "expand_hint",
)
_DIGITS = re.compile(r"\d+")


def _open_readonly(path: str | Path) -> sqlite3.Connection:
    source = Path(path).expanduser()
    if not source.exists():
        raise ValueError(f"migration source does not exist: {source}")
    return sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _require_columns(conn: sqlite3.Connection, table: str,
                     required: tuple[str, ...]) -> None:
    present = _columns(conn, table)
    missing = sorted(set(required) - present)
    if missing:
        raise ValueError(f"LCM {table} schema missing columns: {','.join(missing)}")


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _timestamp(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _clean_text(value: Any, cfg: Config, max_text_chars: int) -> str:
    text = "" if value is None else str(value)
    if cfg.redact_enabled:
        text = redact_text(text, cfg.redact_patterns)
    if len(text) > max_text_chars:
        raise ValueError(
            f"text exceeds UM_MAX_TEXT_CHARS={max_text_chars} during migration")
    return text


def _metadata(value: dict[str, Any], cfg: Config, max_text_chars: int) -> str:
    return _clean_text(json.dumps(value, ensure_ascii=False, sort_keys=True),
                       cfg, max_text_chars)


def _source_ids(raw: Any) -> list[int]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        values = raw
    else:
        text = str(raw).strip()
        try:
            values = json.loads(text)
        except json.JSONDecodeError:
            values = _DIGITS.findall(text)
    if not isinstance(values, list):
        return []
    out: list[int] = []
    for value in values:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item > 0 and item not in out:
            out.append(item)
    return out


def _rows(conn: sqlite3.Connection, table: str,
          columns: tuple[str, ...]) -> list[dict[str, Any]]:
    present = _columns(conn, table)
    selected = [column for column in columns if column in present]
    sql = "SELECT " + ", ".join(selected) + f" FROM {table}"
    return [dict(zip(selected, row)) for row in conn.execute(sql)]


def _read_lcm(path: str | Path) -> tuple[dict[str, int], list[dict[str, Any]],
                                         list[dict[str, Any]], dict[str, str]]:
    conn = _open_readonly(path)
    try:
        _require_columns(conn, "messages", _LCM_MESSAGE_COLUMNS)
        counts = {"messages": _count(conn, "messages")}
        message_columns = list(_LCM_MESSAGE_COLUMNS)
        if "externalized_ref" in _columns(conn, "messages"):
            message_columns.append("externalized_ref")
        messages = _rows(conn, "messages", tuple(message_columns))
        field_policies = {
            "messages.archive_externalized_ref": (
                "preserved_from_source_column" if "externalized_ref" in message_columns
                else "not_present_in_lcm_snapshot"),
        }
        summaries: list[dict[str, Any]] = []
        if _table_exists(conn, "summary_nodes"):
            _require_columns(conn, "summary_nodes", _LCM_SUMMARY_COLUMNS)
            counts["summary_nodes"] = _count(conn, "summary_nodes")
            summaries = _rows(conn, "summary_nodes", _LCM_SUMMARY_COLUMNS)
        messages.sort(key=lambda row: (int(row.get("store_id") or 0),
                                       _timestamp(row.get("timestamp"))))
        summaries.sort(key=lambda row: (int(row.get("node_id") or 0),
                                        _timestamp(row.get("created_at"))))
        return counts, messages, summaries, field_policies
    finally:
        conn.close()


def _lcm_plan(source_path: str | Path, cfg: Config, max_text_chars: int,
              summary_strategy: str) -> dict[str, Any]:
    if summary_strategy not in ("recompute", "preserve"):
        raise ValueError("summary_strategy must be 'recompute' or 'preserve'")
    counts, raw_messages, raw_summaries, field_policies = _read_lcm(source_path)
    messages: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    skipped: dict[str, dict[str, Any]] = {}

    def skip(field: str, reason: str, count: int = 1) -> None:
        item = skipped.setdefault(field, {"count": 0, "reason": reason})
        item["count"] += count

    for row in raw_messages:
        content = _clean_text(row.get("content"), cfg, max_text_chars)
        if not content.strip():
            skip("messages.empty_content", "empty_content")
            continue
        source_id = int(row.get("store_id") or 0)
        metadata = {
            "adapter": "lcm",
            "source_table": "messages",
            "source_id": source_id,
            "tool_call_id": row.get("tool_call_id") or "",
            "tool_name": row.get("tool_name") or "",
            "tool_calls": row.get("tool_calls") or "",
            "token_estimate": row.get("token_estimate"),
            "pinned": row.get("pinned"),
            "ingested_at": row.get("ingested_at"),
            "observed_at": row.get("observed_at"),
            "observed_at_source": row.get("observed_at_source") or "",
        }
        messages.append({
            "source_id": source_id,
            "session_id": str(row.get("session_id") or "default"),
            "source": str(row.get("source") or "unknown"),
            "externalized_ref": (
                _clean_text(row.get("externalized_ref"), cfg, max_text_chars)
                if row.get("externalized_ref") not in (None, "") else None),
            "conversation_id": str(row.get("conversation_id") or ""),
            "role": str(row.get("role") or "user"),
            "content": content,
            "created_at": _timestamp(row.get("timestamp")),
            "source_order": source_id,
            "source_ref": f"lcm:messages:{source_id}",
            "metadata_json": _metadata(metadata, cfg, max_text_chars),
        })

    if summary_strategy == "recompute":
        if raw_summaries:
            skip("summary_nodes", "recompute_by_default", len(raw_summaries))
    else:
        for row in raw_summaries:
            body = _clean_text(row.get("summary"), cfg, max_text_chars)
            if not body.strip():
                skip("summary_nodes.empty_summary", "empty_summary")
                continue
            node_id = int(row.get("node_id") or 0)
            metadata = {
                "adapter": "lcm",
                "source_table": "summary_nodes",
                "source_id": node_id,
                "token_count": row.get("token_count"),
                "source_token_count": row.get("source_token_count"),
                "source_type": row.get("source_type") or "",
                "source_ids": _source_ids(row.get("source_ids")),
                "earliest_at": row.get("earliest_at"),
                "latest_at": row.get("latest_at"),
                "expand_hint": row.get("expand_hint") or "",
            }
            summaries.append({
                "source_id": node_id,
                "session_id": str(row.get("session_id") or "default"),
                "depth": int(row.get("depth") or 0),
                "body": body,
                "created_at": _timestamp(row.get("created_at")),
                "source_ids": _source_ids(row.get("source_ids")),
                "metadata_json": _metadata(metadata, cfg, max_text_chars),
            })

    planned = {"messages": len(messages), "summaries": len(summaries),
               "summary_sources": sum(len(row["source_ids"]) for row in summaries)}
    source_total = sum(counts.values())
    accounted = planned["messages"] + planned["summaries"]
    skipped_count = sum(int(item["count"]) for item in skipped.values())
    return {"source_counts": counts, "messages": messages, "summaries": summaries,
            "planned": planned, "skipped_fields": skipped,
            "field_policies": field_policies,
            "reconciliation": {
                "source_total": source_total,
                "planned_total": accounted,
                "skipped_total": skipped_count,
                "counts_match": source_total == accounted + skipped_count,
            },
            "summary_strategy": summary_strategy}


def _existing_source_id(store: Store, source_ref: str) -> int:
    row = store.conn.execute(
        "SELECT id FROM um_messages WHERE source_ref=? ORDER BY id LIMIT 1",
        (source_ref,)).fetchone()
    return int(row[0]) if row else 0


def _apply_lcm(store: Store, plan: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    inserted = {"messages": 0, "summaries": 0, "summary_sources": 0}
    skipped = 0
    message_map: dict[int, int] = {}
    with store.transaction(dry_run=dry_run):
        for row in plan["messages"]:
            existing = _existing_source_id(store, row["source_ref"])
            if existing:
                message_map[row["source_id"]] = existing
                skipped += 1
                continue
            cur = store.conn.execute(
                "INSERT INTO um_messages(session_id, owner, role, content, created_at,"
                " source, externalized_ref, conversation_id, source_order, source_ref,"
                " metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (row["session_id"], "", row["role"], row["content"],
                 row["created_at"], row["source"], row.get("externalized_ref"),
                 row["conversation_id"], row["source_order"], row["source_ref"],
                 row["metadata_json"]))
            mid = int(cur.lastrowid)
            message_map[row["source_id"]] = mid
            inserted["messages"] += 1
            tokens = estimate_tokens(row["content"])
            store.bump_tokens(row["session_id"], tokens, _commit=False)
            store.bump_raw_tokens(row["session_id"], tokens, _commit=False)

        for row in plan["summaries"]:
            cur = store.conn.execute(
                "INSERT INTO um_summaries(session_id, owner, depth, body, created_at,"
                " metadata_json) VALUES(?,?,?,?,?,?)",
                (row["session_id"], "", row["depth"], row["body"],
                 row["created_at"], row["metadata_json"]))
            sid = int(cur.lastrowid)
            inserted["summaries"] += 1
            tokens = estimate_tokens(row["body"])
            store.bump_tokens(row["session_id"], tokens, _commit=False)
            store.bump_summary_tokens(row["session_id"], tokens, _commit=False)
            for position, source_id in enumerate(row["source_ids"]):
                mid = message_map.get(source_id)
                if not mid:
                    skipped += 1
                    continue
                store.conn.execute(
                    "INSERT OR IGNORE INTO um_summary_sources"
                    "(summary_id, source_table, source_id, position) VALUES(?,?,?,?)",
                    (sid, "um_messages", mid, position))
                inserted["summary_sources"] += 1
        store.rebuild_fts()
    return {"inserted": inserted, "skipped_existing": skipped}


def _recall_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _recall_checks(store: Store, messages: list[dict[str, Any]],
                   inserted_ids: set[int], deferred: bool) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for row in messages[:3]:
        token = next((item for item in tokenize(row["content"])
                      if len(item) >= 3), "")
        if not token:
            continue
        item = {"query_digest": _recall_digest(token)}
        if deferred:
            item["status"] = "deferred_dry_run"
        else:
            hits = store.fts_search(token, scope="all", limit=5)
            item["matched"] = any(
                hit.owner_table == "um_messages" and hit.owner_id in inserted_ids
                for hit in hits)
            item["result_count"] = len(hits)
        checks.append(item)
    return checks


def migrate_lcm(store: Store, source_path: str | Path, *, dry_run: bool = True,
                summary_strategy: str = "recompute", max_text_chars: int | None = None,
                ) -> dict[str, Any]:
    """Plan or atomically apply an LCM SQLite snapshot.

    The report contains counts, field names, and digests only.  Source content
    and tool payloads never enter the report.
    """
    cfg = Config()
    # The target Store already has a Config, but its cfg is intentionally not
    # exposed.  Re-load only redaction/max-text policy; callers can override the
    # cap for deterministic migration tests and bounded operational runs.
    max_chars = cfg.max_text_chars if max_text_chars is None else int(max_text_chars)
    if max_chars < 1:
        raise ValueError("max_text_chars must be >= 1")
    plan = _lcm_plan(source_path, cfg, max_chars, summary_strategy)
    report: dict[str, Any] = {
        "adapter": "lcm",
        "dry_run": bool(dry_run),
        "applied": False,
        "summary_strategy": summary_strategy,
        "source_counts": plan["source_counts"],
        "planned": plan["planned"],
        "skipped_fields": plan["skipped_fields"],
        "field_policies": plan["field_policies"],
    }
    if dry_run:
        report["inserted"] = {key: 0 for key in ("messages", "summaries",
                                                  "summary_sources")}
        report["reconciliation"] = {
            **plan["reconciliation"],
            "applied_total": 0,
            "counts_match": plan["reconciliation"]["counts_match"],
        }
        report["recall_checks"] = _recall_checks(
            store, plan["messages"], set(), True)
        return report

    applied = _apply_lcm(store, plan, dry_run=False)
    report.update(applied)
    report["applied"] = True
    inserted_ids = {int(row[0]) for row in store.conn.execute(
        "SELECT id FROM um_messages WHERE source_ref LIKE 'lcm:messages:%'")}
    report["recall_checks"] = _recall_checks(
        store, plan["messages"], inserted_ids, False)
    report["reconciliation"] = dict(plan["reconciliation"])
    report["reconciliation"].update({
        "applied_total": sum(applied["inserted"].values()),
        "counts_match": (plan["reconciliation"]["counts_match"] and
                         applied["inserted"]["messages"] +
                         applied["inserted"]["summaries"] +
                         applied["skipped_existing"] ==
                         plan["reconciliation"]["source_total"] -
                         plan["reconciliation"]["skipped_total"]),
    })
    return report


_MNEMOSYNE_TABLES = (
    "canonical_facts", "facts", "consolidated_facts", "triples", "graph_edges",
    "memoria_kg", "memoria_facts", "memories", "working_memory",
    "episodic_memory", "scratchpad", "annotations", "memoria_instructions",
    "memoria_preferences", "memoria_timelines", "memoria_persona", "conflicts",
    "memory_validations",
)
_MNEMOSYNE_GRAPH_SKIP = {
    "facts": "derived_projection",
    "memoria_kg": "derived_projection",
    "scratchpad": "not_in_scope",
    "annotations": "not_in_scope",
    "memoria_instructions": "not_in_scope",
    "memoria_preferences": "not_in_scope",
    "memoria_timelines": "not_in_scope",
    "memoria_persona": "not_in_scope",
    "conflicts": "not_in_scope",
    "memory_validations": "not_in_scope",
}
_MNEMOSYNE_COLUMNS = {
    "canonical_facts": (
        "id", "owner_id", "category", "name", "body", "source", "confidence",
        "version", "valid_from", "valid_until", "created_at",
    ),
    "facts": (
        "fact_id", "session_id", "subject", "predicate", "object", "timestamp",
        "source_msg_id", "confidence", "created_at",
    ),
    "consolidated_facts": (
        "id", "subject", "predicate", "object", "confidence", "mention_count",
        "first_seen", "last_seen", "sources_json", "veracity", "superseded_by",
        "created_at", "updated_at",
    ),
    "triples": (
        "id", "subject", "predicate", "object", "valid_from", "valid_until",
        "source", "confidence", "created_at",
    ),
    "graph_edges": (
        "id", "source", "target", "edge_type", "weight", "timestamp", "created_at",
    ),
    "memoria_kg": (
        "id", "session_id", "subject", "predicate", "object", "message_idx",
        "confidence", "source_memory_id",
    ),
    "memoria_facts": (
        "id", "session_id", "message_idx", "fact_type", "key", "value",
        "context_snippet", "importance", "timestamp", "version_id", "previous_value",
        "updated_msg_idx", "valid_from_msg_idx", "valid_to_msg_idx",
        "source_memory_id",
    ),
    "memories": (
        "id", "content", "source", "timestamp", "session_id", "importance",
        "metadata_json", "created_at",
    ),
    "working_memory": (
        "id", "content", "source", "timestamp", "session_id", "importance",
        "metadata_json", "veracity", "created_at", "scope",
    ),
    "episodic_memory": (
        "id", "content", "source", "timestamp", "session_id", "importance",
        "metadata_json", "veracity", "created_at", "scope",
    ),
    "scratchpad": ("id", "content", "session_id", "created_at", "updated_at"),
    "annotations": (
        "id", "memory_id", "kind", "value", "source", "confidence", "created_at",
    ),
}
_MNEMOSYNE_IGNORED_TABLES = tuple(
    table for table in _MNEMOSYNE_TABLES if table not in {
        "canonical_facts", "consolidated_facts", "triples", "graph_edges",
        "memoria_facts", "memories", "working_memory", "episodic_memory",
    }
)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _source_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {"source_metadata": str(raw)}
    return dict(value) if isinstance(value, dict) else {"source_metadata": value}


def _read_mnemosyne(path: str | Path) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]]]:
    conn = _open_readonly(path)
    try:
        counts: dict[str, int] = {}
        rows: dict[str, list[dict[str, Any]]] = {}
        for table in _MNEMOSYNE_TABLES:
            if not _table_exists(conn, table):
                counts[table] = 0
                rows[table] = []
                continue
            columns = _columns(conn, table)
            selected = tuple(column for column in _MNEMOSYNE_COLUMNS.get(table, ())
                             if column in columns)
            counts[table] = _count(conn, table)
            rows[table] = _rows(conn, table, selected) if selected else []
        return counts, rows
    finally:
        conn.close()


def _mapped_owner(source_owner: Any, owner_map: dict[str, str], default_owner: str) -> str:
    raw = str(source_owner or "")
    return str(owner_map.get(raw, raw or default_owner))


def _mnemosyne_plan(source_path: str | Path, cfg: Config, max_text_chars: int,
                    owner_map: dict[str, str], default_owner: str,
                    working_policy: str, episodic_policy: str,
                    memory_policy: str) -> dict[str, Any]:
    policies = {"working_memory": working_policy, "episodic_memory": episodic_policy,
                "memories": memory_policy}
    if any(value not in ("skip", "message") for value in policies.values()):
        raise ValueError("memory policies must be 'skip' or 'message'")
    counts, raw = _read_mnemosyne(source_path)
    facts: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    skipped: dict[str, dict[str, Any]] = {}

    def skip(field: str, reason: str, count: int = 1) -> None:
        if count <= 0:
            return
        item = skipped.setdefault(field, {"count": 0, "reason": reason})
        item["count"] += count

    def clean(value: Any) -> str:
        return _clean_text(value, cfg, max_text_chars)

    def metadata(table: str, source_id: Any, values: dict[str, Any]) -> str:
        return _metadata({"adapter": "mnemosyne", "source_table": table,
                          "source_id": str(source_id), **values},
                         cfg, max_text_chars)

    for row in raw["canonical_facts"]:
        body = clean(row.get("body"))
        if not body.strip():
            skip("canonical_facts.empty_body", "empty_body")
            continue
        category = clean(row.get("category")) or "memory"
        name = clean(row.get("name"))
        if not name:
            skip("canonical_facts.empty_name", "empty_name")
            continue
        source_id = str(row.get("id") or "")
        facts.append({
            "kind": "canonical_fact", "owner": _mapped_owner(
                row.get("owner_id"), owner_map, default_owner),
            "category": category, "name": name, "body": body,
            "importance": 0.5, "created_at": _timestamp(
                row.get("created_at") or row.get("valid_from")),
            "valid_until": _timestamp(row.get("valid_until")),
            "confidence": _safe_float(row.get("confidence"), 1.0),
            "veracity": "", "source_ref": f"mnemosyne:canonical_facts:{source_id}",
            "metadata_json": metadata("canonical_facts", source_id, {
                "source": row.get("source") or "", "version": row.get("version"),
                "valid_from": row.get("valid_from") or "",
            }),
        })
    facts.sort(key=lambda row: (row["created_at"], row["source_ref"]))
    fact_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in facts:
        fact_groups.setdefault((row["owner"], row["category"], row["name"]), []).append(row)
    for group in fact_groups.values():
        for current, following in zip(group, group[1:]):
            current["superseded_ref"] = following["source_ref"]

    for row in raw["memoria_facts"]:
        body = clean(row.get("value"))
        name = clean(row.get("key"))
        if not body.strip() or not name:
            skip("memoria_facts.empty_key_or_value", "empty_key_or_value")
            continue
        source_id = str(row.get("id") or "")
        fact_type = clean(row.get("fact_type")) or "fact"
        facts.append({
            "kind": "memoria_fact", "owner": default_owner,
            "category": f"mnemosyne:{fact_type}", "name": name, "body": body,
            "importance": max(0.0, min(1.0, _safe_float(row.get("importance"), 0.5))),
            "created_at": _timestamp(row.get("timestamp")), "valid_until": 0.0,
            "confidence": 1.0, "veracity": "",
            "source_ref": f"mnemosyne:memoria_facts:{source_id}",
            "metadata_json": metadata("memoria_facts", source_id, {
                "session_id": row.get("session_id") or "", "fact_type": fact_type,
                "message_idx": row.get("message_idx"), "version_id": row.get("version_id"),
                "previous_value": row.get("previous_value") or "",
                "context_snippet": row.get("context_snippet") or "",
                "source_memory_id": row.get("source_memory_id") or "",
            }),
        })

    def add_edge(table: str, row: dict[str, Any], subject: Any, predicate: Any,
                 obj: Any, *, source: Any = "", confidence: Any = 1.0,
                 veracity: Any = "", valid_until: Any = 0.0,
                 created_at: Any = 0.0, session_id: Any = "") -> None:
        subject_text, predicate_text, object_text = clean(subject), clean(predicate), clean(obj)
        if not subject_text or not predicate_text or not object_text:
            skip(f"{table}.incomplete_triple", "incomplete_triple")
            return
        source_id = str(row.get("id") or "")
        edges.append({
            "owner": default_owner, "subject": subject_text,
            "predicate": predicate_text, "object": object_text,
            "session_id": str(session_id or ""), "created_at": _timestamp(created_at),
            "valid_until": _timestamp(valid_until), "confidence": _safe_float(confidence, 1.0),
            "veracity": str(veracity or ""),
            "source_ref": f"mnemosyne:{table}:{source_id}",
            "metadata_json": metadata(table, source_id, {
                "source": source or "", "owner_source": default_owner,
            }),
        })

    seen_triples: set[tuple[str, str, str]] = set()
    for row in raw["triples"]:
        key = (clean(row.get("subject")), clean(row.get("predicate")), clean(row.get("object")))
        seen_triples.add(key)
        add_edge("triples", row, row.get("subject"), row.get("predicate"), row.get("object"),
                 source=row.get("source"), confidence=row.get("confidence", 1.0),
                 valid_until=row.get("valid_until"), created_at=row.get("created_at"))
    for row in raw["consolidated_facts"]:
        key = (clean(row.get("subject")), clean(row.get("predicate")), clean(row.get("object")))
        if key in seen_triples:
            skip("consolidated_facts.duplicate_projection", "duplicate_projection")
            continue
        seen_triples.add(key)
        add_edge("consolidated_facts", row, row.get("subject"), row.get("predicate"),
                 row.get("object"), confidence=row.get("confidence", 0.5),
                 veracity=row.get("veracity"), created_at=row.get("updated_at"))
    for row in raw["graph_edges"]:
        add_edge("graph_edges", row, row.get("source"), row.get("edge_type"),
                 row.get("target"), source="graph_edges", confidence=row.get("weight", 1.0),
                 created_at=row.get("created_at") or row.get("timestamp"))

    for table in _MNEMOSYNE_GRAPH_SKIP:
        skip(table, _MNEMOSYNE_GRAPH_SKIP[table], counts.get(table, 0))
    for table, policy in policies.items():
        if policy == "skip":
            skip(table, "explicit_skip_policy", counts.get(table, 0))

    def add_memory(table: str, row: dict[str, Any]) -> None:
        content = clean(row.get("content"))
        if not content.strip():
            skip(f"{table}.empty_content", "empty_content")
            return
        source_id = str(row.get("id") or "")
        source_meta = _source_metadata(row.get("metadata_json"))
        values = {
            **source_meta, "source": row.get("source") or "",
            "importance": _safe_float(row.get("importance"), 0.5),
            "veracity": row.get("veracity") or "", "scope": row.get("scope") or "",
        }
        messages.append({
            "source_table": table, "source_id": source_id, "content": content,
            "session_id": str(row.get("session_id") or "default"),
            "source": f"mnemosyne:{table}", "created_at": _timestamp(
                row.get("created_at") or row.get("timestamp")),
            "source_order": int(row.get("id") or 0) if str(row.get("id") or "").isdigit() else 0,
            "source_ref": f"mnemosyne:{table}:{source_id}",
            "metadata_json": metadata(table, source_id, values),
        })

    if policies["memories"] == "message":
        for row in raw["memories"]:
            add_memory("memories", row)
    if policies["working_memory"] == "message":
        for row in raw["working_memory"]:
            add_memory("working_memory", row)
    if policies["episodic_memory"] == "message":
        for row in raw["episodic_memory"]:
            add_memory("episodic_memory", row)

    source_total = sum(counts.values())
    planned_total = len(facts) + len(edges) + len(messages)
    skipped_total = sum(int(item["count"]) for item in skipped.values())
    return {
        "source_counts": counts, "facts": facts, "edges": edges, "messages": messages,
        "policies": policies, "skipped_fields": skipped,
        "planned": {"facts": len(facts), "edges": len(edges), "messages": len(messages)},
        "reconciliation": {
            "source_total": source_total, "planned_total": planned_total,
            "skipped_total": skipped_total,
            "counts_match": source_total == planned_total + skipped_total,
        },
    }


def _existing_ref(store: Store, table: str, source_ref: str) -> int:
    row = store.conn.execute(
        f"SELECT id FROM {table} WHERE source_ref=? ORDER BY id LIMIT 1",
        (source_ref,)).fetchone()
    return int(row[0]) if row else 0


def _apply_mnemosyne(store: Store, plan: dict[str, Any]) -> dict[str, Any]:
    inserted = {"facts": 0, "edges": 0, "messages": 0}
    skipped_existing = 0
    fact_ids: dict[str, int] = {}
    with store.transaction():
        for row in plan["facts"]:
            existing = _existing_ref(store, "um_facts", row["source_ref"])
            if existing:
                fact_ids[row["source_ref"]] = existing
                skipped_existing += 1
                continue
            if row["valid_until"] == 0:
                occupied = store.conn.execute(
                    "SELECT id FROM um_facts WHERE owner=? AND category=? AND name=?"
                    " AND valid_until=0", (row["owner"], row["category"], row["name"]),
                ).fetchone()
                if occupied:
                    skipped_existing += 1
                    continue
            cur = store.conn.execute(
                "INSERT INTO um_facts(owner, category, name, body, importance,"
                " created_at, updated_at, valid_until, superseded_by, metadata_json,"
                " confidence, veracity, source_ref) VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?)",
                (row["owner"], row["category"], row["name"], row["body"],
                 row["importance"], row["created_at"], row["created_at"],
                 row["valid_until"], row["metadata_json"], row["confidence"],
                 row["veracity"], row["source_ref"]),
            )
            fact_ids[row["source_ref"]] = int(cur.lastrowid)
            inserted["facts"] += 1
        for row in plan["facts"]:
            source_id = row["source_ref"]
            target = fact_ids.get(source_id)
            successor = fact_ids.get(row.get("superseded_ref", ""))
            if target and successor:
                store.conn.execute(
                    "UPDATE um_facts SET superseded_by=? WHERE id=?", (successor, target))
        for row in plan["edges"]:
            if _existing_ref(store, "um_edges", row["source_ref"]):
                skipped_existing += 1
                continue
            sid = store.add_entity(row["subject"], row["owner"], _commit=False)
            oid = store.add_entity(row["object"], row["owner"], _commit=False)
            cur = store.conn.execute(
                "INSERT INTO um_edges(subject_id, predicate, object_id, session_id,"
                " owner, fact_id, created_at, valid_until, metadata_json, confidence,"
                " veracity, source_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, row["predicate"].lower(), oid, row["session_id"], row["owner"],
                 0, row["created_at"], row["valid_until"], row["metadata_json"],
                 row["confidence"], row["veracity"], row["source_ref"]),
            )
            inserted["edges"] += 1
            del cur
        for row in plan["messages"]:
            if _existing_ref(store, "um_messages", row["source_ref"]):
                skipped_existing += 1
                continue
            store.conn.execute(
                "INSERT INTO um_messages(session_id, owner, role, content, created_at,"
                " source, externalized_ref, conversation_id, source_order, source_ref,"
                " metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (row["session_id"], "", "memory", row["content"], row["created_at"],
                 row["source"], None, "", row["source_order"], row["source_ref"],
                 row["metadata_json"]),
            )
            inserted["messages"] += 1
            tokens = estimate_tokens(row["content"])
            store.bump_tokens(row["session_id"], tokens, _commit=False)
            store.bump_raw_tokens(row["session_id"], tokens, _commit=False)
        store.rebuild_fts()
    return {"inserted": inserted, "skipped_existing": skipped_existing}


def _mnemosyne_recall_checks(store: Store, plan: dict[str, Any], deferred: bool) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    candidates = [(row, "um_facts") for row in plan["facts"]
                 if row["valid_until"] == 0]
    candidates += [(row, "um_messages") for row in plan["messages"]]
    ids = {
        "um_facts": {int(row[0]) for row in store.conn.execute(
            "SELECT id FROM um_facts WHERE source_ref LIKE 'mnemosyne:%'")},
        "um_messages": {int(row[0]) for row in store.conn.execute(
            "SELECT id FROM um_messages WHERE source_ref LIKE 'mnemosyne:%'")},
    }
    for row, table in candidates[:6]:
        token = next((item for item in tokenize(row["body" if table == "um_facts" else "content"])
                      if len(item) >= 3), "")
        if not token:
            continue
        item = {"kind": table, "query_digest": _recall_digest(token)}
        if deferred:
            item["status"] = "deferred_dry_run"
        else:
            hits = store.fts_search(
                token, scope="facts" if table == "um_facts" else "all", limit=5)
            item["matched"] = any(hit.owner_table == table and hit.owner_id in ids[table]
                                   for hit in hits)
            item["result_count"] = len(hits)
        checks.append(item)
    return checks


def migrate_mnemosyne(store: Store, source_path: str | Path, *, dry_run: bool = True,
                      owner_map: dict[str, str] | None = None,
                      default_owner: str = "", working_policy: str = "skip",
                      episodic_policy: str = "skip", memory_policy: str = "skip",
                      max_text_chars: int | None = None) -> dict[str, Any]:
    """Plan or atomically apply a Mnemosyne SQLite snapshot."""
    cfg = Config()
    max_chars = cfg.max_text_chars if max_text_chars is None else int(max_text_chars)
    if max_chars < 1:
        raise ValueError("max_text_chars must be >= 1")
    plan = _mnemosyne_plan(source_path, cfg, max_chars, dict(owner_map or {}),
                            default_owner, working_policy, episodic_policy, memory_policy)
    report: dict[str, Any] = {
        "adapter": "mnemosyne", "dry_run": bool(dry_run), "applied": False,
        "policies": plan["policies"], "source_counts": plan["source_counts"],
        "planned": plan["planned"], "skipped_fields": plan["skipped_fields"],
        "owner_mapping": {"mapped": len(owner_map or {}), "default_owner_present": bool(default_owner)},
    }
    if dry_run:
        report["inserted"] = {key: 0 for key in ("facts", "edges", "messages")}
        report["reconciliation"] = {**plan["reconciliation"], "applied_total": 0}
        report["recall_checks"] = _mnemosyne_recall_checks(store, plan, True)
        return report
    applied = _apply_mnemosyne(store, plan)
    report.update(applied)
    report["applied"] = True
    report["recall_checks"] = _mnemosyne_recall_checks(store, plan, False)
    report["reconciliation"] = dict(plan["reconciliation"])
    report["reconciliation"]["applied_total"] = sum(applied["inserted"].values())
    report["reconciliation"]["counts_match"] = (
        plan["reconciliation"]["counts_match"] and
        report["reconciliation"]["applied_total"] + applied["skipped_existing"] ==
        plan["planned"].get("facts", 0) + plan["planned"].get("edges", 0) +
        plan["planned"].get("messages", 0))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m unified_memory.migration")
    parser.add_argument("--format", choices=("lcm", "mnemosyne"), required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="apply atomically; default is dry-run")
    parser.add_argument("--summary-strategy", choices=("recompute", "preserve"),
                        default="recompute")
    parser.add_argument("--owner-map", default="{}",
                        help="JSON object mapping upstream owner/bank to unified owner")
    parser.add_argument("--default-owner", default="")
    parser.add_argument("--working-policy", choices=("skip", "message"), default="skip")
    parser.add_argument("--episodic-policy", choices=("skip", "message"), default="skip")
    parser.add_argument("--memory-policy", choices=("skip", "message"), default="skip")
    args = parser.parse_args(argv)
    store = Store(load())
    try:
        if args.format == "lcm":
            report = migrate_lcm(store, args.input, dry_run=not args.apply,
                                 summary_strategy=args.summary_strategy)
        else:
            try:
                owner_map = json.loads(args.owner_map)
            except json.JSONDecodeError as exc:
                raise ValueError("--owner-map must be a JSON object") from exc
            if not isinstance(owner_map, dict):
                raise ValueError("--owner-map must be a JSON object")
            report = migrate_mnemosyne(
                store, args.input, dry_run=not args.apply, owner_map=owner_map,
                default_owner=args.default_owner, working_policy=args.working_policy,
                episodic_policy=args.episodic_policy, memory_policy=args.memory_policy)
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
