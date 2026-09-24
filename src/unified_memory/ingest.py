"""Ingest: единый пайплайн записи. Одно сообщение → messages + vectors.

Lossless по умолчанию: compact пишет summaries, сырые сообщения НЕ удаляет
(удаление — только явным mem_forget/prune, которого в v0.1 нет).
"""

from __future__ import annotations

import time

from .config import Config
from .embeddings import EmbeddingBackend
from .engine import ActiveWindow
from .recall import Router
from .store import Store
from .summarize import Summarizer


class Ingest:
    def __init__(self, store: Store, backend: EmbeddingBackend | None = None,
                 summarizer: Summarizer | None = None,
                 cfg: Config | None = None) -> None:
        self.store = store
        self.backend = backend
        self.summarizer = summarizer
        self.cfg = cfg or Config()
        self.window = ActiveWindow(store, summarizer, self.cfg)

    def _clean(self, text: str) -> str:
        """Redaction-гейт + кап длины: весь входящий текст — до SQLite/FTS/vectors."""
        if self.cfg.redact_enabled:
            from .redact import redact_text

            text = redact_text(text, self.cfg.redact_patterns)
        # B10: громкий отказ вместо тихой обрезки; накрывает remember/fact/batch.
        if len(text) > self.cfg.max_text_chars:
            raise ValueError(
                f"text exceeds UM_MAX_TEXT_CHARS={self.cfg.max_text_chars} "
                f"(got {len(text)} chars) — split the payload")
        return text

    def remember_message(self, session_id: str, role: str, content: str,
                         source: str = "mcp", owner: str = "",
                         _commit: bool = True) -> dict:
        if not (content or "").strip():
            raise ValueError("empty content: nothing to remember")
        content = self._clean(content)
        if _commit:
            with self.store.transaction():
                mid = self._remember_message_write(
                    session_id, role, content, source, owner)
        else:
            mid = self._remember_message_write(
                session_id, role, content, source, owner)
        # В batch (_commit=False) компакшн откладывается и делается один раз в конце
        # (guardrail 3): иначе N прогонов по частичному состоянию батча.
        compaction = (self.window.maybe_compact(session_id, owner)
                      if _commit else {"status": "deferred"})
        return {"id": mid, "compaction": compaction}

    def _remember_message_write(self, session_id: str, role: str, content: str,
                                source: str, owner: str) -> int:
        mid = self.store.add_message(session_id, role, content, source, owner,
                                     _commit=False)
        if self.backend is not None:
            self.store.add_vector("um_messages", mid,
                                  self.backend.embed_docs([content])[0],
                                  self.backend.model_name, owner, _commit=False)
        return mid

    def remember_fact(self, category: str, name: str, body: str,
                      importance: float = 0.5, subject: str = "",
                      predicate: str = "", obj: str = "",
                      session_id: str = "", owner: str = "",
                      _commit: bool = True, ttl_s: int | None = None) -> int:
        """Слот-запись факта. Возвращает id живого факта (совместимость)."""
        return self.upsert_fact(category, name, body, importance, subject,
                                predicate, obj, session_id, owner,
                                _commit=_commit, ttl_s=ttl_s)["id"]

    def upsert_fact(self, category: str, name: str, body: str,
                    importance: float = 0.5, subject: str = "",
                    predicate: str = "", obj: str = "",
                    session_id: str = "", owner: str = "",
                    _commit: bool = True, ttl_s: int | None = None) -> dict:
        """Слот-запись + вектора/рёбра. Возвращает {id, status, superseded_id}.

        status: created | superseded (новое тело) | noop (то же тело) |
        updated (только importance). Вектора/рёбра плодим лишь при created/
        superseded — при noop/updated они уже на том же id.
        """
        category, name, body = (self._clean(value) for value in
                                (category, name, body))
        subject, predicate, obj = (self._clean(s) for s in (subject, predicate, obj))
        if ttl_s is None:
            ttl_s = self.cfg.working_ttl_s
        try:
            ttl_s = int(ttl_s)
        except (TypeError, ValueError):
            raise ValueError("ttl_s must be an integer")
        if ttl_s < 0:
            raise ValueError("ttl_s must be >= 0 (0 = keep forever)")
        valid_until = time.time() + ttl_s if category == "working" and ttl_s else 0.0
        if _commit:
            with self.store.transaction():
                out = self._upsert_fact_write(
                    category, name, body, importance, subject, predicate, obj,
                    session_id, owner, valid_until)
        else:
            out = self._upsert_fact_write(
                category, name, body, importance, subject, predicate, obj,
                session_id, owner, valid_until)
        return out

    def _upsert_fact_write(self, category: str, name: str, body: str,
                           importance: float, subject: str, predicate: str,
                           obj: str, session_id: str, owner: str,
                           valid_until: float = 0.0) -> dict:
        out = self.store.add_fact_ex(category, name, body, importance, owner,
                                     _commit=False)
        fid = out["id"]
        if valid_until > 0:
            # A future deadline is metadata for the fact lifecycle, not an
            # immediate expiry: keep the vector and edge alive until the
            # lazy read-path invokes Store.expire_working_facts.
            self.store.conn.execute(
                "UPDATE um_facts SET valid_until=? WHERE id=?",
                (valid_until, fid))
        if out["status"] in ("noop", "updated"):
            return out
        self._embed_fact(fid, _commit=False)
        if subject and predicate and obj:
            eid = self.store.add_edge(subject, predicate, obj, session_id,
                                      fact_id=fid, owner=owner, _commit=False)
            if self.backend is not None:
                self.store.add_vector(
                    "um_edges", eid,
                    self.backend.embed_docs([f"{subject} {predicate} {obj}"])[0],
                    self.backend.model_name, owner, _commit=False)
                for ent in (subject, obj):
                    ent_id = self.store.add_entity(ent, owner, _commit=False)
                    if self.store.has_vector("um_entities", ent_id):
                        continue  # имя то же — вектор тот же, CPU не жжём
                    self.store.add_vector(
                        "um_entities", ent_id,
                        self.backend.embed_docs([ent])[0],
                        self.backend.model_name, owner, _commit=False)
        return out

    def _embed_fact(self, fid: int, _commit: bool = True) -> None:
        """A2: вектор живого факта по актуальному (name, body). No-op без backend."""
        if self.backend is None:
            return
        row = self.store.fact_row(fid)
        if row is None:
            return
        owner, name, body = row
        self.store.add_vector("um_facts", fid,
                              self.backend.embed_docs([f"{name} {body}"])[0],
                              self.backend.model_name, owner, _commit=_commit)

    def update_fact(self, fid: int, body: str | None = None,
                    importance: float | None = None,
                    valid_until: float | None = None,
                    owner: str = "", _commit: bool = True) -> dict | None:
        """Правка факта через Ingest: после store.update_fact переэмбеддить
        новую версию (superseded) или reopened факт — иначе он слеп для
        вектор-плеча (P4.8 удалил вектор при expire)."""
        if body is not None:
            body = self._clean(body)
        if _commit:
            with self.store.transaction():
                out = self._update_fact_write(
                    fid, body, importance, valid_until, owner)
        else:
            out = self._update_fact_write(
                fid, body, importance, valid_until, owner)
        return out

    def _update_fact_write(self, fid: int, body: str | None,
                           importance: float | None, valid_until: float | None,
                           owner: str) -> dict | None:
        out = self.store.update_fact(fid, body=body, importance=importance,
                                     valid_until=valid_until, owner=owner,
                                     _commit=False)
        if out is None:
            return None
        if out["status"] in ("superseded", "reopened"):
            self._embed_fact(out["id"], _commit=False)
        return out

    def batch(self, ops: list[dict], dry_run: bool = False,
              owner: str = "") -> dict:
        """Атомарный батч записей (v0.7-п.5b). Все op в одном контуре; ошибка
        любого → откат всего. Без кросс-ссылок (id op недоступен другому op).

        Каждый op обёрнут в savepoint (инвариант п.5). Компакшн — один раз после
        успешного коммита, ноль при dry-run/rollback.
        """
        results: list[dict] = []
        sessions: set[str] = set()
        error: dict | None = None
        ok = False
        try:
            with self.store.transaction(dry_run=dry_run):
                for i, op in enumerate(ops):
                    with self.store.savepoint():
                        res, sess = self._batch_op(i, op, owner)
                    results.append(res)
                    if sess:
                        sessions.add(sess)
                ok = True
        except Exception as e:  # noqa: BLE001 — контур уже откатил всё
            error = {"index": len(results),
                     "message": f"{type(e).__name__}: {str(e)[:200]}"}
        compactions = []
        if ok and not dry_run:
            for s in sorted(sessions):
                compactions.append({"session_id": s,
                                    "compaction": self.window.maybe_compact(s, owner)})
        return {"ok": ok, "applied": ok and not dry_run, "dry_run": dry_run,
                "results": results, "error": error, "compactions": compactions}

    def _batch_op(self, i: int, op: dict, owner: str) -> tuple[dict, str | None]:
        """Один op батча. Текст — через _clean (redaction-гейт, guardrail п.5)."""
        from .recent import parse_when
        kind = str(op.get("op") or "").strip()
        if kind == "remember":
            sid = str(op.get("session_id", ""))
            out = self.remember_message(
                sid, str(op.get("role", "user")) or "user",
                str(op.get("content", "")), str(op.get("source", "mcp")) or "mcp",
                owner, _commit=False)
            return {"index": i, "op": kind, "status": "remembered",
                    "id": out["id"]}, sid
        if kind == "remember_fact":
            out = self.upsert_fact(
                self._clean(str(op.get("category", ""))),
                self._clean(str(op.get("name", ""))),
                self._clean(str(op.get("body", ""))),
                float(op.get("importance", 0.5)),
                self._clean(str(op.get("subject", ""))),
                self._clean(str(op.get("predicate", ""))),
                self._clean(str(op.get("object", ""))),
                str(op.get("session_id", "")), owner, _commit=False,
                ttl_s=op.get("ttl_s"))
            return {"index": i, "op": kind, "status": out["status"],
                    "id": out["id"], "superseded_id": out["superseded_id"]}, None
        if kind == "update":
            k = str(op.get("kind") or "fact").strip()
            oid = int(op.get("id", 0))
            if k == "fact":
                vu = parse_when(str(op.get("valid_until", ""))) \
                    if op.get("valid_until") not in (None, "") else None
                body = op.get("body")
                body = self._clean(str(body)) if body not in (None, "") else None
                imp = op.get("importance", -1.0)
                out = self.update_fact(
                    oid, body=body,
                    importance=None if imp is None or float(imp) < 0 else float(imp),
                    valid_until=vu, owner=owner, _commit=False)
                if out is None:
                    raise ValueError(f"fact {oid} not found (or owner mismatch)")
                return {"index": i, "op": kind, "kind": k,
                        "status": out["status"], "id": out["id"]}, None
            if k in ("edge", "link"):
                vu = parse_when(str(op.get("valid_until", "")))
                if vu is None:
                    raise ValueError(f"kind={k!r} needs valid_until")
                upd = (self.store.update_edge if k == "edge"
                       else self.store.update_link)
                done = upd(oid, vu, owner, _commit=False)
                return {"index": i, "op": kind, "kind": k, "updated": done,
                        "status": "reopened" if vu == 0 else "expired"}, None
            raise ValueError(f"unknown kind {k!r}: fact | edge | link")
        if kind == "forget":
            k = str(op.get("kind") or "fact").strip()
            oid = int(op.get("id", 0))
            if k == "fact":
                deleted = self.store.delete_fact(oid, owner, _commit=False)
            elif k == "edge":
                deleted = self.store.delete_edge(oid, owner, _commit=False)
            elif k == "link":
                deleted = self.store.delete_link(oid, owner, _commit=False)
            else:
                raise ValueError(f"unknown kind {k!r}: fact | edge | link")
            return {"index": i, "op": kind, "kind": k, "deleted": deleted}, None
        raise ValueError(f"unknown op {kind!r}: remember | remember_fact | update | forget")

    def compact_session(self, session_id: str, keep_tail: int = 20,                        max_sentences: int = 8, owner: str = "") -> dict:
        """Ручное сжатие. Уважает frontier: уже покрытое не дублирует (2.2)."""
        if self.summarizer is None:
            raise ValueError("no summarizer configured")
        from .engine import _mkey

        fkey = _mkey("frontier", session_id, owner)
        frontier = int(self.store.meta_get(fkey) or 0)
        msgs = [m for m in self.store.session_messages(session_id, limit=100000,
                                                       owner=owner)
                if m["id"] > frontier]
        if len(msgs) <= keep_tail:
            return {"status": "noop", "messages": len(msgs)}
        if keep_tail:
            head, tail = msgs[:-keep_tail], msgs[-keep_tail:]
        else:
            head, tail = msgs, []
        try:
            body = self.summarizer.summarize([m["content"] for m in head],
                                             max_sentences=max_sentences)
        except Exception as e:  # noqa: BLE001 — сжатие не роняет вызов
            return {"status": "degraded", "error": f"{type(e).__name__}: {e}"[:200]}
        sid = self.store.add_summary(session_id, body, depth=0,
                                     covers_from=head[0]["id"], covers_to=head[-1]["id"],
                                     owner=owner,
                                     sources=[("um_messages", m["id"])
                                              for m in head])
        self.store.meta_set(fkey, str(head[-1]["id"]))
        return {"status": "compacted", "summary_id": sid,
                "covered": len(head), "kept_tail": len(tail)}

    def router(self) -> Router:
        return Router(self.store, self.backend, self.cfg)

    def _embedding_jobs(self, owner: str = "", missing_only: bool = True
                        ) -> list[tuple[str, int, str, str]]:
        jobs: list[tuple[str, int, str, str]] = []

        def query(sql: str, alias: str) -> list[tuple]:
            sql += " WHERE " + ("v.id IS NULL" if missing_only else "1=1")
            params: tuple = ()
            if owner:
                sql += f" AND {alias}.owner=?"
                params = (owner,)
            return self.store.select(sql, params)

        for oid, content, own, external_ref in query(
                "SELECT m.id, m.content, m.owner, m.externalized_ref"
                " FROM um_messages m"
                " LEFT JOIN um_vectors v ON v.owner_table='um_messages'"
                " AND v.owner_id=m.id", "m"):
            if content and not (external_ref or ""):
                jobs.append(("um_messages", oid, content, own or ""))
        for oid, body, own in query(
                "SELECT s.id, s.body, s.owner FROM um_summaries s"
                " LEFT JOIN um_vectors v ON v.owner_table='um_summaries'"
                " AND v.owner_id=s.id", "s"):
            jobs.append(("um_summaries", oid, body, own or ""))
        now = time.time()
        for oid, name, body, own, valid_until in query(
                "SELECT f.id, f.name, f.body, f.owner, f.valid_until"
                " FROM um_facts f"
                " LEFT JOIN um_vectors v ON v.owner_table='um_facts'"
                " AND v.owner_id=f.id", "f"):
            if float(valid_until or 0.0) != 0 and float(valid_until) <= now:
                continue
            jobs.append(("um_facts", oid, f"{name} {body}", own or ""))
        for oid, sname, pred, oname, own, valid_until in query(
                "SELECT e.id, COALESCE(NULLIF(s.display,''),s.name),"
                " e.predicate, COALESCE(NULLIF(o.display,''),o.name), e.owner,"
                " e.valid_until"
                " FROM um_edges e"
                " JOIN um_entities s ON s.id=e.subject_id"
                " JOIN um_entities o ON o.id=e.object_id"
                " LEFT JOIN um_vectors v ON v.owner_table='um_edges'"
                " AND v.owner_id=e.id", "e"):
            if float(valid_until or 0.0) != 0 and float(valid_until) <= now:
                continue
            jobs.append(("um_edges", oid, f"{sname} {pred} {oname}", own or ""))
        for oid, display, own in query(
                "SELECT e.id, COALESCE(NULLIF(e.display,''),e.name), e.owner"
                " FROM um_entities e"
                " LEFT JOIN um_vectors v ON v.owner_table='um_entities'"
                " AND v.owner_id=e.id", "e"):
            jobs.append(("um_entities", oid, display, own or ""))
        return jobs

    def reindex(self, batch: int = 64, owner: str = "") -> dict:
        """Embed missing vectors without changing the active model."""
        if self.backend is None:
            raise ValueError("no embedding backend: FTS-only store has nothing to reindex")
        model = self.backend.model_name
        report: dict[str, int] = {"embedded": 0, "model_dim": self.backend.dim}
        jobs = self._embedding_jobs(owner=owner, missing_only=True)
        for i in range(0, len(jobs), batch):
            chunk = jobs[i:i + batch]
            vecs = self.backend.embed_docs([text for _, _, text, _ in chunk])
            for (ot, oid, _, own), vec in zip(chunk, vecs):
                self.store.add_vector(ot, oid, vec, model, own)
                report["embedded"] += 1
        if self.cfg.vec_index != "off":
            try:
                report["vec_index"] = self.store.build_vec_index(self.backend.dim)
            except Exception as e:  # noqa: BLE001 — вектора уже доложены
                report["vec_index_error"] = f"{type(e).__name__}: {e}"[:200]
        return report

    def reembed(self, batch: int = 64) -> dict:
        """Atomically replace every source vector with the active backend model."""
        if self.backend is None:
            raise ValueError("no embedding backend: cannot reembed")
        model = self.backend.model_name
        report: dict[str, int] = {"embedded": 0, "model_dim": self.backend.dim}
        jobs = self._embedding_jobs(missing_only=False)
        with self.store.transaction():
            # Disable the old vec0 index while vectors are being replaced.
            self.store.execute_write(
                "DELETE FROM um_meta WHERE key='vec_index_dim'", _commit=False)
            for i in range(0, len(jobs), batch):
                chunk = jobs[i:i + batch]
                vecs = self.backend.embed_docs([text for _, _, text, _ in chunk])
                if len(vecs) != len(chunk):
                    raise ValueError(
                        f"embedding backend returned {len(vecs)} vectors "
                        f"for {len(chunk)} inputs")
                for (ot, oid, _, own), vec in zip(chunk, vecs):
                    if len(vec) != self.backend.dim:
                        raise ValueError(
                            f"embedding dimension mismatch: got {len(vec)}, "
                            f"expected {self.backend.dim}")
                    self.store.add_vector(
                        ot, oid, vec, model, own, _commit=False,
                        _allow_model_mismatch=True)
                    report["embedded"] += 1
            self.store.meta_set("embedding_model", model, _commit=False)
            self.store.meta_set("embedding_dim", str(self.backend.dim), _commit=False)
        if self.cfg.vec_index != "off":
            try:
                report["vec_index"] = self.store.build_vec_index(self.backend.dim)
            except Exception as e:  # noqa: BLE001 — vectors are already replaced
                report["vec_index_error"] = f"{type(e).__name__}: {e}"[:200]
        return report
