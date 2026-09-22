"""Ingest: единый пайплайн записи. Одно сообщение → messages + vectors.

Lossless по умолчанию: compact пишет summaries, сырые сообщения НЕ удаляет
(удаление — только явным mem_forget/prune, которого в v0.1 нет).
"""

from __future__ import annotations

from .embeddings import EmbeddingBackend
from .recall import Router
from .store import Store
from .summarize import Summarizer


class Ingest:
    def __init__(self, store: Store, backend: EmbeddingBackend | None = None,
                 summarizer: Summarizer | None = None) -> None:
        self.store = store
        self.backend = backend
        self.summarizer = summarizer

    def remember_message(self, session_id: str, role: str, content: str,
                         source: str = "mcp") -> int:
        mid = self.store.add_message(session_id, role, content, source)
        if self.backend is not None:
            self.store.add_vector("um_messages", mid,
                                  self.backend.embed_docs([content])[0],
                                  self.backend.spec.name)
        return mid

    def remember_fact(self, category: str, name: str, body: str,
                      importance: float = 0.5) -> int:
        fid = self.store.add_fact(category, name, body, importance)
        if self.backend is not None:
            self.store.add_vector("um_facts", fid,
                                  self.backend.embed_docs([f"{name} {body}"])[0],
                                  self.backend.spec.name)
        return fid

    def compact_session(self, session_id: str, keep_tail: int = 20,
                        max_sentences: int = 8) -> dict:
        """Сжать старые сообщения сессии в summary-ноду. Сырьё остаётся."""
        if self.summarizer is None:
            raise ValueError("no summarizer configured")
        msgs = self.store.session_messages(session_id, limit=100000)
        if len(msgs) <= keep_tail:
            return {"status": "noop", "messages": len(msgs)}
        head, tail = msgs[:-keep_tail], msgs[-keep_tail:]
        body = self.summarizer.summarize([m["content"] for m in head],
                                         max_sentences=max_sentences)
        sid = self.store.add_summary(session_id, body, depth=0,
                                     covers_from=head[0]["id"], covers_to=head[-1]["id"])
        return {"status": "compacted", "summary_id": sid,
                "covered": len(head), "kept_tail": len(tail)}

    def router(self) -> Router:
        return Router(self.store, self.backend)
