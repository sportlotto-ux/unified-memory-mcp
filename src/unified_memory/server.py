"""MCP server: unified memory for Hermes / Claude Code / any MCP client.

Tools: mem_remember mem_fact mem_annotate mem_link mem_graph_query mem_recall
       mem_expand mem_get mem_inspect mem_compact mem_forget mem_status mem_doctor
       mem_validate mem_task mem_persona

Run: python -m unified_memory.server  (stdio transport)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

try:
    import unified_memory  # noqa: F401 — установленный пакет или -m: путь не трогаем
except ImportError:  # прямой запуск файлом: src/unified_memory/server.py
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from mcp.server.mcpserver import MCPServer as _Server

from mcp.types import ToolAnnotations  # noqa: E402

from unified_memory.config import load  # noqa: E402
from unified_memory.embeddings import make_backend  # noqa: E402
from unified_memory.ingest import Ingest  # noqa: E402
from unified_memory.store import Store  # noqa: E402
from unified_memory.summarize import default_summarizer  # noqa: E402
from unified_memory import archive  # noqa: E402
from unified_memory.recent import parse_as_of, parse_period, parse_when  # noqa: E402
from unified_memory.evidence import (  # noqa: E402
    parse_ref, run_cite, run_compute, run_conflicts, run_validate)

mcp = _Server("unified-memory")


def _ann(ro: bool = False, destr: bool = False, idem: bool = False,
         ow: bool = False) -> ToolAnnotations:
    """C11: поведенческие хинты тула (v0.7.3). Все тулы — локальный стор (ow=False)."""
    return ToolAnnotations(read_only_hint=ro, destructive_hint=destr,
                           idempotent_hint=idem, open_world_hint=ow)

_STATE = {"ingest": None, "store": None, "cfg": None, "backend_error": None}
_INIT_LOCK = threading.Lock()  # FastMCP гоняет sync-тулы в тредах: двойная проверка


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
        with _INIT_LOCK:  # два первых вызова в тредах не должны строить Store дважды
            if _STATE["ingest"] is None:
                cfg = load()
                try:
                    backend = _backend(cfg)
                except Exception as e:  # noqa: BLE001 — любой сбой warm = FTS-only
                    backend = None
                    _STATE["backend_error"] = f"{type(e).__name__}: {e}"[:300]
                store = Store(cfg,
                              embedding_dim=backend.dim if backend else 0,
                              embedding_model=(getattr(backend, "model_name",
                                                       cfg.embedding_model)
                                                if backend else ""))
                _STATE.update(store=store, cfg=cfg,
                              ingest=Ingest(store, backend, default_summarizer(), cfg))
                _maybe_maintenance(store, cfg)  # ленивый weekly-purge + порог архива
    return _STATE["ingest"]


def _maybe_maintenance(store, cfg) -> None:
    """Связка retention→архив (вариант i), никогда не роняет тул.

    retention>0: раз в неделю шаг (а) — горячее старше N дней в архив.
    Затем (б) — если размер всё ещё > порога, добить oldest до порога.
    retention=0: только (б). Авто-удаления НЕТ: purge_older_than — вручную.
    Любой сбой уходит в um_meta.maintenance_error, а не наружу.
    """
    try:
        now = time.time()
        size_limit = cfg.archive_size_mb * 1024 * 1024
        if cfg.retention_days > 0:
            last = float(store.meta_get("retention_last_run") or 0)
            if now - last >= 7 * 86400:
                cutoff = now - cfg.retention_days * 86400
                conn = archive.open_archive(cfg.archive_path)
                try:
                    moved = archive.move_oldest(
                        store, conn, cfg.archive_batch, before_ts=cutoff,
                        label=str(cfg.archive_path))
                finally:
                    conn.close()
                store.meta_set("retention_last_run", str(now))
                store.meta_set("retention_last_moved", str(moved))
        # (б) страховка от быстрого роста — работает и при retention=0
        if store.db_size_bytes() >= size_limit:
            total = 0
            conn = archive.open_archive(cfg.archive_path)
            try:
                for _ in range(10):  # bounded: не более 10 батчей за проход
                    moved = archive.move_oldest(
                        store, conn, cfg.archive_batch, label=str(cfg.archive_path))
                    total += moved
                    if moved == 0 or store.db_size_bytes() < size_limit:
                        break
            finally:
                conn.close()
            store.meta_set("archive_last_run", str(now))
            store.meta_set("archive_last_moved", str(total))
        # B9: держим WAL от неограниченного роста (best-effort, never-raises).
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:  # noqa: BLE001 — обслуживание не должно ломать тулы
        try:
            store.meta_set("maintenance_error", f"{type(e).__name__}: {e}"[:200])
        except Exception:
            pass


def _store():
    _ingest()
    return _STATE["store"]


@mcp.tool(annotations=_ann())
def mem_remember(session_id: str = "default", role: str = "user",
                 content: str = "", owner: str = "") -> str:
    """Save a session message. Auto-compacts past the pressure threshold."""
    return json.dumps(_ingest().remember_message(session_id, role, content,
                                                 owner=owner))


@mcp.tool(annotations=_ann())
def mem_fact(category: str, name: str, body: str,
             importance: float = 0.5, subject: str = "",
             predicate: str = "", object: str = "",
             session_id: str = "", owner: str = "",
             ttl_s: int | None = None) -> str:
    """Save a long-term fact, optionally with a graph triple and working TTL."""
    return json.dumps({"id": _ingest().remember_fact(
        category, name, body, importance, subject, predicate, object,
        session_id, owner, ttl_s=ttl_s)})


@mcp.tool(annotations=_ann(idem=True))
def mem_link(src: str, dst: str, rel: str, weight: float = 1.0,
             session_id: str = "", owner: str = "") -> str:
    """Typed memory link (ADR-001). src/dst like 'fact:3' | 'message:12' |
    'summary:2' | 'edge:5'. rel: supports | contradicts | supersedes | derives_from.
    Both endpoints must exist and belong to `owner`. Re-linking a live
    (src,dst,rel,owner) is a no-op returning the existing id (created=false)."""
    _ingest()
    st, sid = parse_ref(src)
    dt, did = parse_ref(dst)
    out = _store().link(st, sid, dt, did, rel, weight, session_id, owner)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool(annotations=_ann(idem=True))
def mem_annotate(target: str, kind: str, value: str = "",
                 source: str = "", confidence: float = 1.0,
                 owner: str = "") -> str:
    """Annotate a ref (P2.2). target like 'fact:3' | 'message:12' |
    'summary:2' | 'edge:5'. kind: useful | disputed | correction | note.
    Target must exist and belong to `owner`. Same (target,kind,value,owner)
    is a no-op returning the existing id. Metadata-only: recall unchanged."""
    _ingest()
    st, sid = parse_ref(target)
    out = _STATE["ingest"].annotate(st, sid, kind, value, source,
                                    confidence, owner)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_graph_query(subject: str = "", predicate: str = "", object: str = "",
                    rel: str = "", min_weight: float = 0.0,
                    max_hops: int = 1, limit: int = 100,
                    owner: str = "", session_id: str = "",
                    include_expired: bool = False, as_of: str = "") -> str:
    """Bounded graph traversal over entity edges and typed links.

    subject/predicate/object are exact (case-insensitive) entity-edge filters;
    rel/min_weight filter typed links. as_of selects a validity slice, while
    include_expired exposes historical liveness. owner/session are hard scopes.
    """
    _ingest()
    cfg = _STATE["cfg"]
    if max_hops < 1:
        raise ValueError("max_hops must be >= 1")
    if max_hops > cfg.recall_max_hops:
        raise ValueError(
            f"max_hops={max_hops} exceeds UM_RECALL_MAX_HOPS={cfg.recall_max_hops}")
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    if min_weight < 0:
        raise ValueError("min_weight must be >= 0")
    as_of_ts = parse_as_of(as_of) if as_of else None
    out = _store().graph_query(
        subject=subject, predicate=predicate, object=object, session_id=session_id,
        owner=owner, rel=rel, min_weight=min_weight, max_hops=max_hops,
        include_expired=include_expired, as_of=as_of_ts, limit=limit)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_recall(query: str, scope: str = "all", session_id: str = "",
               limit: int = 10, owner: str = "",
               include_expired: bool = False, as_of: str = "",
               hops: int = 1, rel: str = "",
               diagnostics: bool = False, source: str = "",
               include_archived: bool = False) -> str:
    """Unified search: FTS + vectors + RRF. Scope: all | session | facts.
    Истёкшие (valid_until) прячутся (include_expired=True — аудит истории).
    as_of (ISO-date) — срез графа на дату: valid_from <= as_of < valid_until.
    hops>1 — BFS по типизированным связям и entity-графу (ADR-001); rel фильтрует
    связи (`supports`/`contradicts`/`supersedes`/`derives_from`; на рёбрах — predicate).
    source фильтрует только messages по их source; facts/summaries/graph не имеют
    source dimension и исключаются из такого recall.
    include_archived=true добавляет bounded lexical search по cold archive;
    default false сохраняет hot-only recall.
    bounded importance component для facts включается конфигом UM_IMPORTANCE_WEIGHT;
    default 0 сохраняет legacy ranking, diagnostics показывает factor contribution.
    diagnostics=true → {"hits": [...], "diagnostics": {arms/contrib/timings/bfs/importance/degraded}};
    false — ровно прежний список (аддитивность)."""
    ing = _ingest()
    cfg = _STATE["cfg"]
    if hops < 1:
        raise ValueError("hops must be >= 1")
    if hops > cfg.recall_max_hops:
        raise ValueError(
            f"hops={hops} exceeds UM_RECALL_MAX_HOPS={cfg.recall_max_hops}")
    as_of_ts = parse_as_of(as_of) if as_of else None
    router = ing.router()
    hits = router.recall(query, scope, session_id, limit, owner,
                         include_expired, as_of_ts, hops, rel,
                         diagnostics=diagnostics, source=source,
                         include_archived=include_archived)
    out = []
    for h in hits:
        if h.snippet and len(h.body) > 2000:  # A4: сниппет только для ДЛИННЫХ FTS-тел
            body = h.snippet
        else:  # короткие — verbatim; прочие плечи — прежний head-truncate
            body = h.body[:2000]
            if len(h.body) > 2000:
                body += "…[truncated, use mem_expand for full text]"
        item = {"kind": h.owner_table, "id": h.owner_id,
                "score": round(h.score, 4), "session": h.session_id,
                "body": body}
        if h.archived:
            item["archived"] = True
            item["archive_ref"] = h.extra
        out.append(item)
    if diagnostics:
        return json.dumps({"hits": out,
                           "diagnostics": router.last_stats.get("diagnostics", {})},
                          ensure_ascii=False)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_expand(kind: str, id: int, owner: str = "") -> str:
    """Verbatim fetch. kind: message | fact | summary | edge. Uniform schema."""
    if kind == "message":
        msg = _store().get_message(int(id), owner)
        if msg and msg.get("externalized_ref"):
            cfg = _STATE["cfg"]
            conn = archive.open_archive(cfg.archive_path)
            try:
                arc = archive.fetch_message(conn, int(id))
            finally:
                conn.close()
            body = arc["content"] if arc else None
            return json.dumps({"kind": kind, "id": int(id), "body": body,
                               "archived": True,
                               "externalized_ref": msg["externalized_ref"]},
                              ensure_ascii=False)
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
    out = {"kind": kind, "id": int(id), "body": body, **meta}
    if kind == "summary":
        out["lineage"] = _store().summary_lineage(int(id), owner)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_get(kind: str, id: int, owner: str = "") -> str:
    """Read one message/fact/summary/edge with metadata and direct links."""
    table = {"message": "um_messages", "fact": "um_facts",
             "summary": "um_summaries", "edge": "um_edges"}.get(kind)
    if table is None:
        raise ValueError(
            f"unknown kind {kind!r}: message | fact | summary | edge")
    details = _store().ref_details(table, int(id), owner)
    if details is None:
        return json.dumps({"kind": kind, "id": int(id), "found": False},
                          ensure_ascii=False)
    body = details["body"]
    archived = False
    if kind == "message" and details["metadata"].get("externalized_ref"):
        cfg = _STATE["cfg"]
        conn = archive.open_archive(cfg.archive_path)
        try:
            arc = archive.fetch_message(conn, int(id))
        finally:
            conn.close()
        body = arc["content"] if arc else None
        archived = True
    return json.dumps({
        "kind": kind, "id": int(id), "found": True, "body": body,
        "archived": archived, "metadata": details["metadata"],
        "vector": details["vector"], "links": details["links"],
        "annotations": details.get("annotations", []),
    }, ensure_ascii=False)


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_inspect(session_id: str = "", summary_id: int = 0,
                message_id: int = 0, owner: str = "") -> str:
    """Read-only store/session diagnostics without returning all message bodies."""
    ing = _ingest()
    cfg = _STATE["cfg"]
    out = {
        "store": {
            **ing.store.stats(),
            "diagnostics": ing.store.diagnostics(),
            "hygiene": ing.store.hygiene(),
        },
        "archive": archive.audit(ing.store, cfg.archive_path),
    }
    if session_id:
        p = ing.window.pressure(session_id, owner)
        fkey = (f"frontier:{owner}:{session_id}" if owner
                else f"frontier:{session_id}")
        nodes = []
        for row in ing.store.select(
                "SELECT id, depth, covers_from, covers_to, superseded_by"
                " FROM um_summaries WHERE session_id=?"
                + (" AND owner=?" if owner else "") + " ORDER BY id",
                (session_id, owner) if owner else (session_id,)):
            nodes.append(dict(zip(
                ["id", "depth", "covers_from", "covers_to", "superseded_by"],
                row)))
        out["session"] = {
            "session_id": session_id,
            "owner": owner,
            "frontier": int(ing.store.meta_get(fkey) or 0),
            "pressure": {
                "tokens_total": p.tokens_total,
                "raw_backlog_tokens": p.raw_backlog_tokens,
                "active_summary_tokens": p.active_summary_tokens,
                "compactable_tokens": p.compactable_tokens,
                "threshold_tokens": p.threshold_tokens,
                "over": p.over,
                "messages": p.messages,
                "summaries": p.summaries,
            },
            "summary_nodes": nodes,
        }
    if summary_id:
        details = ing.store.ref_details("um_summaries", int(summary_id), owner)
        out["summary"] = details
    if message_id:
        details = ing.store.ref_details("um_messages", int(message_id), owner)
        out["message"] = details
    return json.dumps(out, ensure_ascii=False)


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_load_session(session_id: str, after_id: int = 0, limit: int = 50,
                     owner: str = "") -> str:
    """Paginate the lossless session transcript; archived rows return archive refs."""
    if not session_id:
        raise ValueError("session_id is required")
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    items = _store().session_transcript(
        session_id, after_id=int(after_id), limit=limit, owner=owner)
    next_after = items[-1]["id"] if len(items) == limit else 0
    return json.dumps({
        "session_id": session_id,
        "owner": owner,
        "after_id": int(after_id),
        "items": items,
        "next_after_id": next_after,
        "has_more": len(items) == limit,
    }, ensure_ascii=False)


@mcp.tool(annotations=_ann())
def mem_update(kind: str = "fact", id: int = 0, body: str = "",
               importance: float = -1.0, valid_until: str = "",
               owner: str = "") -> str:
    """Edit a fact by id (new version, keeps history) or expire/reopen fact|edge|link.
    valid_until: "" = unchanged, "open" = reopen (0), "now" | ISO-date | epoch = expire."""
    store = _store()
    vu = parse_when(valid_until) if kind in ("fact", "edge", "link") else None
    if kind == "fact":
        out = _ingest().update_fact(
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
    if kind == "link":
        if vu is None:
            raise ValueError("kind='link' needs valid_until ('open' or a date)")
        ok = store.update_link(int(id), vu, owner)
        return json.dumps({"id": int(id), "updated": ok,
                           "status": "reopened" if vu == 0 else "expired"})
    raise ValueError(f"unknown kind {kind!r}: fact | edge | link")


@mcp.tool(annotations=_ann(idem=True))
def mem_compact(session_id: str, keep_tail: int = 20, owner: str = "") -> str:
    """Summarize old session messages. Raw messages are kept (lossless)."""
    return json.dumps(_ingest().compact_session(session_id, keep_tail,
                                                owner=owner))


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_assemble(session_id: str, budget: int = 0, owner: str = "",
                 include_working: bool = False) -> str:
    """Bounded active context, optionally including a capped working slice."""
    return json.dumps(_ingest().window.assemble(
        session_id, budget, owner, include_working=include_working),
        ensure_ascii=False)


@mcp.tool(annotations=_ann(destr=True, idem=True))
def mem_forget(id: str = "", kind: str = "fact", owner: str = "") -> str:
    """Delete by kind: fact (numeric id), edge (numeric id), link (numeric id), annotation (numeric id), entity (name)."""
    if kind in ("fact", "edge", "link", "annotation"):
        try:
            oid = int(id)
        except (TypeError, ValueError):
            raise ValueError(f"kind={kind!r} needs a numeric id, got {id!r}")
        if kind == "fact":
            return json.dumps({"deleted": _store().delete_fact(oid, owner)})
        if kind == "edge":
            return json.dumps({"deleted": _store().delete_edge(oid, owner)})
        if kind == "annotation":
            return json.dumps({"deleted": _store().delete_annotation(oid, owner)})
        return json.dumps({"deleted": _store().delete_link(oid, owner)})
    if kind == "entity":
        if not (id or "").strip():
            raise ValueError("kind='entity' needs a name")
        return json.dumps({"deleted": _store().delete_entity(id, owner)})
    raise ValueError(f"unknown kind {kind!r}: fact | edge | link | annotation | entity")


@mcp.tool(annotations=_ann(idem=True))
def mem_reindex(owner: str = "") -> str:
    """Embed owners missing vectors (ladder after a model change). Idempotent."""
    return json.dumps(_ingest().reindex(owner=owner))


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_recent(period: str = "today", session_id: str = "",
               owner: str = "", limit: int = 20,
               before_id: int = 0, before_ts: float = 0.0,
               before_kind: str = "") -> str:
    """Temporal: what happened in a UTC window. Period: today | yesterday | week | month | Nd | date:YYYY-MM-DD | last Nh.
    Paging: pass the returned next.before_ts + next.before_id + next.before_kind to get the next (older) page."""
    window = parse_period(period)
    items = _store().recent(window.start_ts, window.end_ts, session_id, owner, limit,
                            before_ts=before_ts, before_id=before_id,
                            before_kind=before_kind)
    out = []
    for it in items:
        body = it["body"][:2000]
        if len(it["body"]) > 2000:
            body += "…[truncated, use mem_expand for full text]"
        out.append({"kind": it["kind"], "id": it["id"], "session": it["session_id"],
                    "created_at": it["created_at"], "body": body})
    nxt = ({"before_ts": items[-1]["created_at"], "before_id": items[-1]["id"],
            "before_kind": items[-1]["kind"]}
           if len(items) == limit and items else None)
    return json.dumps({"period": period, "window": {
        "start": window.start_ts, "end": window.end_ts}, "items": out,
        "next": nxt}, ensure_ascii=False)


def _archive_fetch(cfg):
    """Callback для evidence: тянет вынесенный текст сообщения из архива."""
    if not cfg.archive_path.exists():  # чтение не должно создавать архив
        return None

    def fetch(table: str, oid: int):
        if table != "um_messages":
            return None
        conn = archive.open_archive(cfg.archive_path)
        try:
            got = archive.fetch_message(conn, oid)
            return got["content"] if got else None
        finally:
            conn.close()

    return fetch


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_evidence(claim: str = "", refs: list[str] | None = None,
                 mode: str = "cite", op: str = "count",
                 pattern: str = "", owner: str = "") -> str:
    """Verify a claim against refs (mode=cite → supported|partial|unsupported)
    or aggregate numbers over refs (mode=compute, op=count|sum|min|max|avg|median).
    mode=conflicts → verdict-free кандидаты противоречий (needs_judgment).
    Only the refs you pass are used — no auto-search. refs like 'fact:3'."""
    _ingest()
    cfg = _STATE["cfg"]
    refs = refs or []
    if mode == "cite":
        out = run_cite(_STATE["store"], claim, refs, owner=owner,
                       max_refs=cfg.evidence_max_refs,
                       max_chars=cfg.evidence_max_chars,
                       partial=cfg.evidence_partial,
                       archived_fetch=_archive_fetch(cfg))
    elif mode == "compute":
        out = run_compute(_STATE["store"], refs, op=op, pattern=pattern,
                          owner=owner, max_refs=cfg.evidence_max_refs,
                          max_chars=cfg.evidence_max_chars,
                          archived_fetch=_archive_fetch(cfg))
    elif mode == "conflicts":
        out = run_conflicts(_STATE["store"], refs, owner=owner,
                            max_refs=cfg.evidence_max_refs,
                            max_chars=cfg.evidence_max_chars,
                            archived_fetch=_archive_fetch(cfg))
    else:
        raise ValueError(f"unknown mode {mode!r}: cite | compute | conflicts")
    return json.dumps(out, ensure_ascii=False)


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_validate(target: str, claim: str = "", owner: str = "") -> str:
    """Collate validation signals for one ref (P2.3): cite (if claim) +
    conflicts (target + direct supports/contradicts neighbours) + live
    supports/contradicts links + annotations. Verdict-free: needs_judgment
    is always true, the host agent judges. target like 'fact:3'."""
    _ingest()
    cfg = _STATE["cfg"]
    out = run_validate(_STATE["store"], target, claim, owner=owner,
                       max_refs=cfg.evidence_max_refs,
                       max_chars=cfg.evidence_max_chars,
                       partial=cfg.evidence_partial,
                       archived_fetch=_archive_fetch(cfg))
    return json.dumps(out, ensure_ascii=False)


@mcp.tool(annotations=_ann())
def mem_persona(op: str = "get", trait: str = "", body: str = "",
                limit: int = 100, owner: str = "") -> str:
    """Agent profile over slot-facts (P2.5). op=set (trait+body upsert →
    created|superseded|noop) | op=get (whole live profile {trait: body},
    ordered by trait, capped by limit). Traits are category='persona' facts:
    mem_get/mem_forget(kind=fact) apply."""
    ing = _ingest()
    if op == "set":
        out = ing.persona_set(trait, body, owner)
        return json.dumps({"id": out["id"], "trait": (trait or "").strip(),
                           "status": out["status"],
                           "superseded_id": out.get("superseded_id", 0)},
                          ensure_ascii=False)
    if op == "get":
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        traits = ing.persona_profile(owner, limit)
        return json.dumps({"traits": traits, "count": len(traits)},
                          ensure_ascii=False)
    raise ValueError(f"unknown op {op!r}: set | get")


@mcp.tool(annotations=_ann())
def mem_task(op: str = "list", name: str = "", body: str = "",
             status: str = "", id: int = 0, limit: int = 50,
             owner: str = "") -> str:
    """Task progress over slot-facts (P2.4). op=create (name+body → open) |
    op=status (id+status: open/doing/blocked/done, done reopens only to open) |
    op=list (live tasks, optional status filter). Tasks are category='task'
    facts: mem_get/mem_annotate/mem_validate/mem_forget(kind=fact) apply."""
    ing = _ingest()
    if op == "create":
        return json.dumps(ing.task_create(name, body, owner),
                          ensure_ascii=False)
    if op == "status":
        return json.dumps(ing.task_status(int(id), status, owner),
                          ensure_ascii=False)
    if op == "list":
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        return json.dumps({"tasks": ing.task_list(owner, status, limit)},
                          ensure_ascii=False)
    raise ValueError(f"unknown op {op!r}: create | status | list")


@mcp.tool(annotations=_ann(destr=True))
def mem_batch(ops: list[dict] | None = None, dry_run: bool = False,
              owner: str = "") -> str:
    """Atomic batch of writes (all-or-nothing). ops: list of
    {"op":"remember", session_id, role?, content, source?}
    | {"op":"remember_fact", category, name, body, importance?, subject?, predicate?, object?, session_id?}
    | {"op":"update", kind:"fact|edge|link", id, body?, importance?, valid_until?}
    | {"op":"forget", kind:"fact|edge|link", id}.
    dry_run=true validates then rolls back (applied=false). No cross-refs: ids from
    one op can't be used by another. Caps UM_BATCH_MAX_OPS / UM_BATCH_MAX_CHARS are
    checked before the transaction opens."""
    ing = _ingest()
    cfg = _STATE["cfg"]
    ops = ops or []
    if not ops:
        raise ValueError("ops must be a non-empty list")
    if len(ops) > cfg.batch_max_ops:
        raise ValueError(
            f"len(ops)={len(ops)} exceeds UM_BATCH_MAX_OPS={cfg.batch_max_ops}")
    payload = len(json.dumps(ops, ensure_ascii=False))
    if payload > cfg.batch_max_chars:
        raise ValueError(
            f"ops payload {payload} chars exceeds "
            f"UM_BATCH_MAX_CHARS={cfg.batch_max_chars}")
    return json.dumps(ing.batch(ops, dry_run=dry_run, owner=owner),
                      ensure_ascii=False)


def _wal_bytes(db_path) -> int:
    """B9: размер -wal рядом с БД (0, если нет). Витрина перед checkpoint."""
    try:
        return os.path.getsize(str(db_path) + "-wal")
    except OSError:
        return 0


def _secret_scan(store, patterns, cap: int = 50) -> dict:
    """B8: read-only скан горячего стора каталогом redaction.

    Отчёт только `{pattern, kind, id}` — БЕЗ значений (иначе сканер сам утечка).
    """
    from unified_memory.redact import PATTERNS

    active = [p for p in patterns if p in PATTERNS]
    sources = [("um_messages", "content"), ("um_facts", "body"),
               ("um_summaries", "body"), ("um_entities", "name"),
               ("um_edges", "predicate"), ("um_annotations", "value")]
    hits: list = []
    total = 0
    with store.read_locked():  # проход мимо _locked-методов: лочим явно
        for table, col in sources:
            for oid, text in store.conn.execute(
                    f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL"):
                if not text:
                    continue
                for name in active:
                    if name == "private_key" and "PRIVATE KEY" not in text.upper():
                        continue
                    if PATTERNS[name].search(text):
                        total += 1
                        if len(hits) < cap:
                            hits.append({"pattern": name, "kind": table, "id": oid})
    return {"hits": hits, "total": total, "cap": cap, "patterns": active}


@mcp.tool(annotations=_ann(ro=True, idem=True))
def mem_status() -> str:
    """Store stats and degradation flags."""
    ing = _ingest()
    cfg = _STATE["cfg"]
    if cfg.archive_path.exists():  # чтение не должно создавать архив
        conn = archive.open_archive(cfg.archive_path)
        try:
            arch = archive.status(conn, cfg.archive_path)
        finally:
            conn.close()
    else:
        arch = {"archive_path": str(cfg.archive_path), "archive_bytes": 0,
                "archived_messages": 0, "archived_vectors": 0}
    return json.dumps({**ing.store.stats(),
                       "vectors_enabled": ing.backend is not None,
                       "embedding_backend": cfg.embedding_backend,
                       "embedding_model": (getattr(ing.backend, "model_name",
                                                     cfg.embedding_model)
                                           if ing.backend else cfg.embedding_model),
                       "embedding_dim": ing.backend.dim if ing.backend else 0,
                       "redaction_enabled": cfg.redact_enabled,
                       "redaction_patterns": list(cfg.redact_patterns),
                       "backend_error": _STATE["backend_error"],
                       "summarizer": type(default_summarizer()).__name__,
                       "db": str(cfg.db_path),
                       "db_size_bytes": ing.store.db_size_bytes(),
                       "wal_bytes": _wal_bytes(cfg.db_path),
                       "retention_days": cfg.retention_days,
                       "retention_last_run": ing.store.meta_get("retention_last_run"),
                       "maintenance_error": ing.store.meta_get("maintenance_error"),
                       "archive": arch})


@mcp.tool(annotations=_ann(destr=True))
def mem_doctor(mode: str = "check", apply: bool = False) -> str:
    """DB diagnostics. mode: check (readonly: diagnostics + hygiene candidates) |
    export (readonly JSON dump to <db>.export-<ts>.json) |
    archive_check (readonly: заглушки ↔ архив, orphans) |
    secret_scan (readonly: каталог redaction по горячему стору, отчёт без значений) |
    retention (age-based: move hot messages older than UM_RETENTION_DAYS to archive) |
    clean (purge orphans) | repair (purge + FTS rebuild + vec rebuild).
    clean/repair/retention требуют apply=True (иначе dry-run) и всегда backup-first."""
    store = _store()
    if mode == "check":
        return json.dumps({**store.diagnostics(), "hygiene": store.hygiene()})
    if mode == "export":
        from unified_memory.export import export_store
        return json.dumps(export_store(store), ensure_ascii=False)
    if mode == "archive_check":  # B7: read-only сверка заглушек с архивом
        return json.dumps({"mode": mode,
                           **archive.audit(store, _STATE["cfg"].archive_path)})
    if mode == "secret_scan":  # B8: read-only скан, отчёт без значений
        return json.dumps({"mode": mode,
                           **_secret_scan(store, _STATE["cfg"].redact_patterns)})
    if mode not in ("clean", "repair", "archive", "purge", "retention"):
        raise ValueError(
            f"unknown mode {mode!r}: check | export | archive_check | secret_scan"
            " | clean | repair | archive | purge | retention")
    cfg = _STATE["cfg"]
    if mode == "archive":
        if not apply:
            return json.dumps({"mode": mode, "apply_required": True,
                               "would_archive": len(store.oldest_messages(cfg.archive_batch)),
                               "db_size_bytes": store.db_size_bytes()})
        conn = archive.open_archive(cfg.archive_path)
        try:
            moved = archive.move_oldest(store, conn, cfg.archive_batch,
                                        label=str(cfg.archive_path))
            st = archive.status(conn, cfg.archive_path)
        finally:
            conn.close()
        return json.dumps({"mode": mode, "moved": moved, **st})
    if mode == "purge":
        if cfg.retention_days <= 0:
            return json.dumps({"mode": mode, "skipped": "retention_days=0 (keep forever)"})
        cutoff = time.time() - cfg.retention_days * 86400
        if not apply:
            if not cfg.archive_path.exists():  # dry-run не создаёт архив
                return json.dumps({"mode": mode, "apply_required": True,
                                   "would_purge": 0})
            conn = archive.open_archive(cfg.archive_path)
            try:
                n = conn.execute(
                    "SELECT count(*) FROM ar_messages WHERE created_at < ?",
                    (cutoff,)).fetchone()[0]
            finally:
                conn.close()
            return json.dumps({"mode": mode, "apply_required": True,
                               "would_purge": n})
        conn = archive.open_archive(cfg.archive_path)
        try:
            purged = archive.purge_older_than(conn, cutoff)
        finally:
            conn.close()
        return json.dumps({"mode": mode, "purged": purged})
    if mode == "retention":
        # (а) age-based проход: вынести горячее старше retention_days (для cron).
        if cfg.retention_days <= 0:
            return json.dumps({"mode": mode,
                               "skipped": "retention_days=0 (keep forever)"})
        before_ts = time.time() - cfg.retention_days * 86400
        would = store.select(
            "SELECT count(*) FROM um_messages WHERE"
            " (externalized_ref IS NULL OR externalized_ref='')"
            " AND created_at < ?", (before_ts,))[0][0]
        if not apply:  # dry-run не создаёт архив
            return json.dumps({"mode": mode, "apply_required": True,
                               "would_move": would, "before_ts": before_ts})
        if would == 0:  # нечего двигать — архив не открываем (P3.1-стиль)
            return json.dumps({"mode": mode, "moved": 0, "before_ts": before_ts})
        conn = archive.open_archive(cfg.archive_path)
        try:
            moved = 0
            while True:  # до полного опустошения среза — идемпотентный второй прогон
                n = archive.move_oldest(store, conn, cfg.archive_batch, before_ts,
                                        label=str(cfg.archive_path))
                moved += n
                if n == 0:
                    break
            st = archive.status(conn, cfg.archive_path)
        finally:
            conn.close()
        return json.dumps({"mode": mode, "moved": moved,
                           "before_ts": before_ts, **st})
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
