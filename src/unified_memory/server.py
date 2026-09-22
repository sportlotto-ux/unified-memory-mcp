"""MCP server: unified memory for Hermes / Claude Code / any MCP client.

Tools: mem_remember mem_fact mem_recall mem_expand
       mem_compact mem_forget mem_status mem_doctor

Run: python -m unified_memory.server  (stdio transport)
"""

from __future__ import annotations

import json
import os
import sys

try:
    import unified_memory  # noqa: F401 — установленный пакет или -m: путь не трогаем
except ImportError:  # прямой запуск файлом: src/unified_memory/server.py
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

try:  # mcp 2.x: FastMCP renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

from unified_memory.config import load  # noqa: E402
from unified_memory.embeddings import make_backend  # noqa: E402
from unified_memory.ingest import Ingest  # noqa: E402
from unified_memory.store import Store  # noqa: E402
from unified_memory.summarize import default_summarizer  # noqa: E402
from unified_memory.recent import parse_as_of, parse_period, parse_when  # noqa: E402

mcp = _Server("unified-memory")

_STATE = {"ingest": None, "store": None, "cfg": None, "backend_error": None}


def _backend(cfg):
    # local без fastembed → ImportError на warm; openai без сервера →
    # EmbedServerError на warm. Оба ловит _ingest: сервер стартует FTS-only,
    # причина видна в mem_status.backend_error. Молчаливых нулей нет.
    b = make_backend(cfg)
    b.warm()  # проба связи + детект dim (openai) / загрузка модели (local)
    return b


def _ingest():
    """#7: ленивая инициализация. Ошибка сети/модели роняет вектора, не сервер."""
    if _STATE["ingest"] is None:
        cfg = load()
        try:
            backend = _backend(cfg)
        except Exception as e:  # noqa: BLE001 — любой сбой warm = FTS-only
            backend = None
            _STATE["backend_error"] = f"{type(e).__name__}: {e}"[:300]
        store = Store(cfg,
                      embedding_dim=backend.dim if backend else 0,
                      embedding_model=cfg.embedding_model if backend else "")
        _STATE.update(store=store, cfg=cfg,
                      ingest=Ingest(store, backend, default_summarizer(), cfg))
    return _STATE["ingest"]


def _store():
    _ingest()
    return _STATE["store"]


@mcp.tool()
def mem_remember(session_id: str = "default", role: str = "user",
                 content: str = "", owner: str = "") -> str:
    """Save a session message. Auto-compacts past the pressure threshold."""
    return json.dumps(_ingest().remember_message(session_id, role, content,
                                                 owner=owner))


@mcp.tool()
def mem_fact(category: str, name: str, body: str,
             importance: float = 0.5, subject: str = "",
             predicate: str = "", object: str = "",
             session_id: str = "", owner: str = "") -> str:
    """Save a long-term fact, optionally with a graph triple. Returns its id."""
    return json.dumps({"id": _ingest().remember_fact(
        category, name, body, importance, subject, predicate, object,
        session_id, owner)})


@mcp.tool()
def mem_recall(query: str, scope: str = "all", session_id: str = "",
               limit: int = 10, owner: str = "",
               include_expired: bool = False, as_of: str = "") -> str:
    """Unified search: FTS + vectors + RRF. Scope: all | session | facts.
    Истёкшие (valid_until) прячутся (include_expired=True — аудит истории).
    as_of (ISO-date) — срез графа на дату: valid_from <= as_of < valid_until."""
    as_of_ts = parse_as_of(as_of) if as_of else None
    hits = _ingest().router().recall(query, scope, session_id, limit, owner,
                                     include_expired, as_of_ts)
    out = []
    for h in hits:
        body = h.body[:2000]
        if len(h.body) > 2000:
            body += "…[truncated, use mem_expand for full text]"
        out.append({"kind": h.owner_table, "id": h.owner_id,
                    "score": round(h.score, 4), "session": h.session_id,
                    "body": body})
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def mem_expand(kind: str, id: int, owner: str = "") -> str:
    """Verbatim fetch. kind: message | fact | summary | edge. Uniform schema."""
    if kind == "message":
        msg = _store().get_message(int(id), owner)
        return json.dumps({"kind": kind, "id": int(id),
                           "body": msg["content"] if msg else None},
                          ensure_ascii=False)
    table = {"fact": "um_facts", "summary": "um_summaries",
             "edge": "um_edges"}.get(kind)
    if table is None:
        raise ValueError(f"unknown kind {kind!r}: message | fact | summary | edge")
    if owner and _store().owners_for([(table, int(id))]).get((table, int(id)), "") != owner:
        return json.dumps({"kind": kind, "id": int(id), "body": None}, ensure_ascii=False)
    body, _ = _store()._body_of(table, int(id))
    meta = _store().row_meta(table, int(id))  # valid_until / superseded_by
    return json.dumps({"kind": kind, "id": int(id), "body": body, **meta},
                      ensure_ascii=False)


@mcp.tool()
def mem_update(kind: str = "fact", id: int = 0, body: str = "",
               importance: float = -1.0, valid_until: str = "",
               owner: str = "") -> str:
    """Edit a fact by id (new version, keeps history) or expire/reopen fact|edge.
    valid_until: "" = unchanged, "open" = reopen (0), "now" | ISO-date | epoch = expire."""
    store = _store()
    vu = parse_when(valid_until) if kind in ("fact", "edge") else None
    if kind == "fact":
        out = store.update_fact(
            int(id), body=body or None,
            importance=None if importance < 0 else importance,
            valid_until=vu, owner=owner)
        if out is None:
            raise ValueError(f"fact {id} not found (or owner mismatch)")
        return json.dumps(out)
    if kind == "edge":
        if vu is None:
            raise ValueError("kind='edge' needs valid_until ('open' or a date)")
        ok = store.update_edge(int(id), vu, owner)
        return json.dumps({"id": int(id), "updated": ok,
                           "status": "reopened" if vu == 0 else "expired"})
    raise ValueError(f"unknown kind {kind!r}: fact | edge")


@mcp.tool()
def mem_compact(session_id: str, keep_tail: int = 20, owner: str = "") -> str:
    """Summarize old session messages. Raw messages are kept (lossless)."""
    return json.dumps(_ingest().compact_session(session_id, keep_tail,
                                                owner=owner))


@mcp.tool()
def mem_assemble(session_id: str, budget: int = 0, owner: str = "") -> str:
    """Bounded active context: ready summaries + fresh tail within budget."""
    return json.dumps(_ingest().window.assemble(session_id, budget, owner),
                      ensure_ascii=False)


@mcp.tool()
def mem_forget(id: str = "", kind: str = "fact", owner: str = "") -> str:
    """Delete by kind: fact (numeric id), edge (numeric id), entity (name)."""
    if kind in ("fact", "edge"):
        try:
            oid = int(id)
        except (TypeError, ValueError):
            raise ValueError(f"kind={kind!r} needs a numeric id, got {id!r}")
        if kind == "fact":
            return json.dumps({"deleted": _store().delete_fact(oid, owner)})
        return json.dumps({"deleted": _store().delete_edge(oid, owner)})
    if kind == "entity":
        if not (id or "").strip():
            raise ValueError("kind='entity' needs a name")
        return json.dumps({"deleted": _store().delete_entity(id, owner)})
    raise ValueError(f"unknown kind {kind!r}: fact | edge | entity")


@mcp.tool()
def mem_reindex(owner: str = "") -> str:
    """Embed owners missing vectors (ladder after a model change). Idempotent."""
    return json.dumps(_ingest().reindex(owner=owner))


@mcp.tool()
def mem_recent(period: str = "today", session_id: str = "",
               owner: str = "", limit: int = 20) -> str:
    """Temporal: what happened in a UTC window. Period: today | yesterday | week | month | Nd | date:YYYY-MM-DD | last Nh."""
    window = parse_period(period)
    items = _store().recent(window.start_ts, window.end_ts, session_id, owner, limit)
    out = []
    for it in items:
        body = it["body"][:2000]
        if len(it["body"]) > 2000:
            body += "…[truncated, use mem_expand for full text]"
        out.append({"kind": it["kind"], "id": it["id"], "session": it["session_id"],
                    "created_at": it["created_at"], "body": body})
    return json.dumps({"period": period, "window": {
        "start": window.start_ts, "end": window.end_ts}, "items": out},
        ensure_ascii=False)


@mcp.tool()
def mem_status() -> str:
    """Store stats and degradation flags."""
    ing = _ingest()
    cfg = _STATE["cfg"]
    return json.dumps({**ing.store.stats(),
                       "vectors_enabled": ing.backend is not None,
                       "embedding_backend": cfg.embedding_backend,
                       "embedding_model": cfg.embedding_model,
                       "embedding_dim": ing.backend.dim if ing.backend else 0,
                       "redaction_enabled": cfg.redact_enabled,
                       "redaction_patterns": list(cfg.redact_patterns),
                       "backend_error": _STATE["backend_error"],
                       "summarizer": type(default_summarizer()).__name__,
                       "db": str(cfg.db_path)})


@mcp.tool()
def mem_doctor(mode: str = "check", apply: bool = False) -> str:
    """DB diagnostics. mode: check (readonly: diagnostics + hygiene candidates) |
    clean (purge orphans) | repair (purge + FTS rebuild + vec rebuild).
    clean/repair требуют apply=True (иначе dry-run) и всегда backup-first."""
    store = _store()
    if mode == "check":
        return json.dumps({**store.diagnostics(), "hygiene": store.hygiene()})
    if mode not in ("clean", "repair"):
        raise ValueError(f"unknown mode {mode!r}: check | clean | repair")
    if not apply:
        return json.dumps({"mode": mode, "apply_required": True,
                           "would": store.hygiene()})
    if mode == "clean":
        dirty = store.hygiene()
        rep = store.repair(dim=0)
        rep["vec_skipped"] = True
        return json.dumps({**rep, "candidates": dirty})
    ing = _ingest()
    dim = ing.backend.dim if ing.backend else 0
    return json.dumps(store.repair(dim=dim))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
