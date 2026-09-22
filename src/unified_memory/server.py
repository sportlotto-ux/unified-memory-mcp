"""MCP server: unified memory for Hermes / Claude Code / any MCP client.

Tools: mem_remember mem_fact mem_recall mem_expand
       mem_compact mem_forget mem_status mem_doctor

Run: python -m unified_memory.server  (stdio transport)
"""

from __future__ import annotations

import json
import os
import sys

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


def _backend():
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return None  # FTS-only режим, флаг виден в mem_status
    cfg = load()
    b = FastembedBackend(model=cfg.embedding_model)
    b.warm()
    return b


_BACKEND = _backend()
_CFG = load()
_STORE = Store(_CFG,
               embedding_dim=_BACKEND.dim if _BACKEND else 0,
               embedding_model=_CFG.embedding_model if _BACKEND else "")
_INGEST = Ingest(_STORE, _BACKEND, default_summarizer(), _CFG)


@mcp.tool()
def mem_remember(session_id: str = "default", role: str = "user",
                 content: str = "") -> str:
    """Save a session message. Auto-compacts past the pressure threshold."""
    return json.dumps(_INGEST.remember_message(session_id, role, content))


@mcp.tool()
def mem_fact(category: str, name: str, body: str,
             importance: float = 0.5, subject: str = "",
             predicate: str = "", object: str = "",
             session_id: str = "") -> str:
    """Save a long-term fact, optionally with a graph triple. Returns its id."""
    return json.dumps({"id": _INGEST.remember_fact(
        category, name, body, importance, subject, predicate, object, session_id)})


@mcp.tool()
def mem_recall(query: str, scope: str = "all", session_id: str = "",
               limit: int = 10) -> str:
    """Unified search: FTS + vectors + RRF. Scope: all | session | facts."""
    hits = _INGEST.router().recall(query, scope, session_id, limit)
    return json.dumps([{"kind": h.owner_table, "id": h.owner_id,
                        "score": round(h.score, 4), "session": h.session_id,
                        "body": h.body[:2000]} for h in hits], ensure_ascii=False)


@mcp.tool()
def mem_expand(kind: str, id: int) -> str:
    """Verbatim fetch. kind: message | fact | summary | edge."""
    if kind == "message":
        return json.dumps(_STORE.get_message(int(id)), ensure_ascii=False)
    table = {"fact": "um_facts", "summary": "um_summaries",
             "edge": "um_edges"}[kind]
    body, _ = _STORE._body_of(table, int(id))
    return json.dumps({"kind": kind, "id": int(id), "body": body}, ensure_ascii=False)


@mcp.tool()
def mem_compact(session_id: str, keep_tail: int = 20) -> str:
    """Summarize old session messages. Raw messages are kept (lossless)."""
    return json.dumps(_INGEST.compact_session(session_id, keep_tail))


@mcp.tool()
def mem_assemble(session_id: str, budget: int = 0) -> str:
    """Bounded active context: ready summaries + fresh tail within budget."""
    return json.dumps(_INGEST.window.assemble(session_id, budget),
                      ensure_ascii=False)


@mcp.tool()
def mem_forget(id: int) -> str:
    """Delete a fact by id."""
    return json.dumps({"deleted": _STORE.delete_fact(int(id))})


@mcp.tool()
def mem_status() -> str:
    """Store stats and degradation flags."""
    return json.dumps({**_STORE.stats(), "vectors_enabled": _BACKEND is not None,
                       "summarizer": type(default_summarizer()).__name__,
                       "db": str(_CFG.db_path)})


@mcp.tool()
def mem_doctor() -> str:
    """DB diagnostics: integrity, vectors by model, FTS flag."""
    integ = _STORE.conn.execute("PRAGMA integrity_check").fetchone()[0]
    vec_rows = _STORE.conn.execute(
        "SELECT model, count(*) FROM um_vectors GROUP BY model").fetchall()
    return json.dumps({"integrity": integ, "vectors_by_model": vec_rows,
                       "fts": _STORE.fts})


def main():
    mcp.run()


if __name__ == "__main__":
    main()
