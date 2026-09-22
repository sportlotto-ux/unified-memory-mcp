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
        """Redaction-гейт: весь входящий текст — через него до SQLite/FTS/vectors."""
        if self.cfg.redact_enabled:
            from .redact import redact_text

            return redact_text(text, self.cfg.redact_patterns)
        return text

    def remember_message(self, session_id: str, role: str, content: str,
                         source: str = "mcp", owner: str = "") -> dict:
        if not (content or "").strip():
            raise ValueError("empty content: nothing to remember")
        content = self._clean(content)
        mid = self.store.add_message(session_id, role, content, source, owner)
        if self.backend is not None:
            self.store.add_vector("um_messages", mid,
                                  self.backend.embed_docs([content])[0],
                                  self.backend.model_name, owner)
        return {"id": mid,
                "compaction": self.window.maybe_compact(session_id, owner)}

    def remember_fact(self, category: str, name: str, body: str,
                      importance: float = 0.5, subject: str = "",
                      predicate: str = "", obj: str = "",
                      session_id: str = "", owner: str = "") -> int:
        name, body = self._clean(name), self._clean(body)
        subject, predicate, obj = (self._clean(s) for s in (subject, predicate, obj))
        fid = self.store.add_fact(category, name, body, importance, owner)
        if self.backend is not None:
            self.store.add_vector("um_facts", fid,
                                  self.backend.embed_docs([f"{name} {body}"])[0],
                                  self.backend.model_name, owner)
        if subject and predicate and obj:
            eid = self.store.add_edge(subject, predicate, obj, session_id,
                                      fact_id=fid, owner=owner)
            if self.backend is not None:
                self.store.add_vector(
                    "um_edges", eid,
                    self.backend.embed_docs([f"{subject} {predicate} {obj}"])[0],
                    self.backend.model_name, owner)
                for ent in (subject, obj):
                    ent_id = self.store.add_entity(ent, owner)
                    if self.store.has_vector("um_entities", ent_id):
                        continue  # имя то же — вектор тот же, CPU не жжём
                    self.store.add_vector(
                        "um_entities", ent_id,
                        self.backend.embed_docs([ent])[0],
                        self.backend.model_name, owner)
        return fid

    def compact_session(self, session_id: str, keep_tail: int = 20,
                        max_sentences: int = 8, owner: str = "") -> dict:
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
