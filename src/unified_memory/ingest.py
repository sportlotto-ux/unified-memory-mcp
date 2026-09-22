"""Ingest: единый пайплайн записи. Одно сообщение → messages + vectors.

Lossless по умолчанию: compact пишет summaries, сырые сообщения НЕ удаляет
(удаление — только явным mem_forget/prune, которого в v0.1 нет).
"""

from __future__ import annotations

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
        mid = self.store.add_message(session_id, role, content, source, owner,
                                     _commit=_commit)
        if self.backend is not None:
            self.store.add_vector("um_messages", mid,
                                  self.backend.embed_docs([content])[0],
                                  self.backend.model_name, owner, _commit=_commit)
        # В batch (_commit=False) компакшн откладывается и делается один раз в конце
        # (guardrail 3): иначе N прогонов по частичному состоянию батча.
        compaction = (self.window.maybe_compact(session_id, owner)
                      if _commit else {"status": "deferred"})
        return {"id": mid, "compaction": compaction}

    def remember_fact(self, category: str, name: str, body: str,
                      importance: float = 0.5, subject: str = "",
                      predicate: str = "", obj: str = "",
                      session_id: str = "", owner: str = "",
                      _commit: bool = True) -> int:
        """Слот-запись факта. Возвращает id живого факта (совместимость)."""
        return self.upsert_fact(category, name, body, importance, subject,
                                predicate, obj, session_id, owner,
                                _commit=_commit)["id"]

    def upsert_fact(self, category: str, name: str, body: str,
                    importance: float = 0.5, subject: str = "",
                    predicate: str = "", obj: str = "",
                    session_id: str = "", owner: str = "",
                    _commit: bool = True) -> dict:
        """Слот-запись + вектора/рёбра. Возвращает {id, status, superseded_id}.

        status: created | superseded (новое тело) | noop (то же тело) |
        updated (только importance). Вектора/рёбра плодим лишь при created/
        superseded — при noop/updated они уже на том же id.
        """
        name, body = self._clean(name), self._clean(body)
        subject, predicate, obj = (self._clean(s) for s in (subject, predicate, obj))
        out = self.store.add_fact_ex(category, name, body, importance, owner,
                                     _commit=_commit)
        fid = out["id"]
        if out["status"] in ("noop", "updated"):
            return out
        if self.backend is not None:
            self.store.add_vector("um_facts", fid,
                                  self.backend.embed_docs([f"{name} {body}"])[0],
                                  self.backend.model_name, owner, _commit=_commit)
        if subject and predicate and obj:
            eid = self.store.add_edge(subject, predicate, obj, session_id,
                                      fact_id=fid, owner=owner, _commit=_commit)
            if self.backend is not None:
                self.store.add_vector(
                    "um_edges", eid,
                    self.backend.embed_docs([f"{subject} {predicate} {obj}"])[0],
                    self.backend.model_name, owner, _commit=_commit)
                for ent in (subject, obj):
                    ent_id = self.store.add_entity(ent, owner, _commit=_commit)
                    if self.store.has_vector("um_entities", ent_id):
                        continue  # имя то же — вектор тот же, CPU не жжём
                    self.store.add_vector(
                        "um_entities", ent_id,
                        self.backend.embed_docs([ent])[0],
                        self.backend.model_name, owner, _commit=_commit)
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
        if kind == "remember_fact":
            out = self.upsert_fact(
                self._clean(str(op.get("category", ""))),
                self._clean(str(op.get("name", ""))),
                self._clean(str(op.get("body", ""))),
                float(op.get("importance", 0.5)),
                self._clean(str(op.get("subject", ""))),
                self._clean(str(op.get("predicate", ""))),
                self._clean(str(op.get("object", ""))),
                str(op.get("session_id", "")), owner, _commit=False)
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
                out = self.store.update_fact(
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
        raise ValueError(f"unknown op {kind!r}: remember_fact | update | forget")

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
                                     owner=owner)
        self.store.meta_set(fkey, str(head[-1]["id"]))
        return {"status": "compacted", "summary_id": sid,
                "covered": len(head), "kept_tail": len(tail)}

    def router(self) -> Router:
        return Router(self.store, self.backend, self.cfg)

    def reindex(self, batch: int = 64, owner: str = "") -> dict:
        """Лестница после смены модели: довложить вектора, которых нет.

        Идёмпотентен: трогает только owner без векторов. Чужие dim не трогает
        (их надо удалять руками — это уже миграция, не reindex).
        """
        if self.backend is None:
            raise ValueError("no embedding backend: FTS-only store has nothing to reindex")
        model = self.backend.model_name
        report: dict[str, int] = {"embedded": 0, "model_dim": self.backend.dim}
        jobs: list[tuple[str, int, str]] = []
        for oid, content in self.store.select(
                """SELECT m.id, m.content FROM um_messages m
                   LEFT JOIN um_vectors v ON v.owner_table='um_messages' AND v.owner_id=m.id
                   WHERE v.id IS NULL"""
                + (" AND m.owner=?" if owner else ""), (owner,) if owner else ()):
            jobs.append(("um_messages", oid, content))
        for oid, name, body in self.store.select(
                """SELECT f.id, f.name, f.body FROM um_facts f
                   LEFT JOIN um_vectors v ON v.owner_table='um_facts' AND v.owner_id=f.id
                   WHERE v.id IS NULL"""
                + (" AND f.owner=?" if owner else ""), (owner,) if owner else ()):
            jobs.append(("um_facts", oid, f"{name} {body}"))
        for eid, sname, pred, oname in self.store.select(
                """SELECT e.id, s.display, e.predicate, o.display FROM um_edges e
                   JOIN um_entities s ON s.id=e.subject_id
                   JOIN um_entities o ON o.id=e.object_id
                   LEFT JOIN um_vectors v ON v.owner_table='um_edges' AND v.owner_id=e.id
                   WHERE v.id IS NULL"""
                + (" AND e.owner=?" if owner else ""), (owner,) if owner else ()):
            jobs.append(("um_edges", eid, f"{sname} {pred} {oname}"))
        for ent_id, display in self.store.select(
                """SELECT e.id, e.display FROM um_entities e
                   LEFT JOIN um_vectors v ON v.owner_table='um_entities' AND v.owner_id=e.id
                   WHERE v.id IS NULL"""
                + (" AND e.owner=?" if owner else ""), (owner,) if owner else ()):
            jobs.append(("um_entities", ent_id, display or ""))
        for i in range(0, len(jobs), batch):
            chunk = jobs[i:i + batch]
            vecs = self.backend.embed_docs([text for _, _, text in chunk])
            for (ot, oid, _), vec in zip(chunk, vecs):
                self.store.add_vector(ot, oid, vec, model, owner)
                report["embedded"] += 1
        if self.cfg.vec_index != "off":
            try:
                report["vec_index"] = self.store.build_vec_index(self.backend.dim)
            except Exception as e:  # noqa: BLE001 — нет sqlite-vec: вектора уже доложены
                report["vec_index_error"] = f"{type(e).__name__}: {e}"[:200]
        return report
