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
from unified_memory.embeddings import FastembedBackend  # noqa: E402
from unified_memory.ingest import Ingest  # noqa: E402
from unified_memory.store import Store  # noqa: E402
from unified_memory.summarize import default_summarizer  # noqa: E402

mcp = _Server("unified-memory")

_STATE = {"ingest": None, "store": None, "cfg": None, "backend_error": None}


def _backend(cfg):
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return None  # FTS-only режим, флаг виден в mem_status
    b = FastembedBackend(model=cfg.embedding_model)
    b.warm()  # может кинуть на битой сети — ловим ниже, сервер стартует
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
                 content: str = "") -> str:
    """Save a session message. Auto-compacts past the pressure threshold."""
    return json.dumps(_ingest().remember_message(session_id, role, content))


@mcp.tool()
def mem_fact(category: str, name: str, body: str,
             importance: float = 0.5, subject: str = "",
             predicate: str = "", object: str = "",
             session_id: str = "") -> str:
    """Save a long-term fact, optionally with a graph triple. Returns its id."""
    return json.dumps({"id": _ingest().remember_fact(
        category, name, body, importance, subject, predicate, object, session_id)})


@mcp.tool()
def mem_recall(query: str, scope: str = "all", session_id: str = "",
               limit: int = 10) -> str:
    """Unified search: FTS + vectors + RRF. Scope: all | session | facts."""
    hits = _ingest().router().recall(query, scope, session_id, limit)
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
def mem_expand(kind: str, id: int) -> str:
    """Verbatim fetch. kind: message | fact | summary | edge. Uniform schema."""
    if kind == "message":
        msg = _store().get_message(int(id))
        return json.dumps({"kind": kind, "id": int(id),
                           "body": msg["content"] if msg else None},
                          ensure_ascii=False)
    table = {"fact": "um_facts", "summary": "um_summaries",
             "edge": "um_edges"}.get(kind)
    if table is None:
        raise ValueError(f"unknown kind {kind!r}: message | fact | summary | edge")
    body, _ = _store()._body_of(table, int(id))
    return json.dumps({"kind": kind, "id": int(id), "body": body}, ensure_ascii=False)


@mcp.tool()
def mem_compact(session_id: str, keep_tail: int = 20) -> str:
    """Summarize old session messages. Raw messages are kept (lossless)."""
    return json.dumps(_ingest().compact_session(session_id, keep_tail))


@mcp.tool()
def mem_assemble(session_id: str, budget: int = 0) -> str:
    """Bounded active context: ready summaries + fresh tail within budget."""
    return json.dumps(_ingest().window.assemble(session_id, budget),
                      ensure_ascii=False)


@mcp.tool()
def mem_forget(id: str = "", kind: str = "fact") -> str:
    """Delete by kind: fact (numeric id), edge (numeric id), entity (name)."""
    if kind in ("fact", "edge"):
        try:
            oid = int(id)
        except (TypeError, ValueError):
            raise ValueError(f"kind={kind!r} needs a numeric id, got {id!r}")
        if kind == "fact":
            return json.dumps({"deleted": _store().delete_fact(oid)})
        return json.dumps({"deleted": _store().delete_edge(oid)})
    if kind == "entity":
        if not (id or "").strip():
            raise ValueError("kind='entity' needs a name")
        return json.dumps({"deleted": _store().delete_entity(id)})
    raise ValueError(f"unknown kind {kind!r}: fact | edge | entity")


@mcp.tool()
def mem_reindex() -> str:
    """Embed owners missing vectors (ladder after a model change). Idempotent."""
    return json.dumps(_ingest().reindex())


@mcp.tool()
def mem_status() -> str:
    """Store stats and degradation flags."""
    ing = _ingest()
    return json.dumps({**ing.store.stats(),
                       "vectors_enabled": ing.backend is not None,
                       "backend_error": _STATE["backend_error"],
                       "summarizer": type(default_summarizer()).__name__,
                       "db": str(_STATE["cfg"].db_path)})


@mcp.tool()
def mem_doctor() -> str:
    """DB diagnostics: integrity, vectors by model, FTS flag."""
    return json.dumps(_store().diagnostics())


def main():
    mcp.run()


if __name__ == "__main__":
    main()
