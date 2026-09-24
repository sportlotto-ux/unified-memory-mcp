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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m unified_memory.migration")
    parser.add_argument("--format", choices=("lcm",), required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="apply atomically; default is dry-run")
    parser.add_argument("--summary-strategy", choices=("recompute", "preserve"),
                        default="recompute")
    args = parser.parse_args(argv)
    store = Store(load())
    try:
        report = migrate_lcm(store, args.input, dry_run=not args.apply,
                             summary_strategy=args.summary_strategy)
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
