"""Recall router: FTS + vectors + RRF fusion.

Один recall вместо трёх (lcm_recall / lcm_grep / mnemosyne_recall).
Без векторов (fastembed не стоит) — чистый FTS, флаг виден в mem_status.
"""

from __future__ import annotations

from .embeddings import EmbeddingBackend
from .store import Hit, Store, cosine

_RRF_K = 60


def rrf_fuse(rank_lists: list[list[Hit]], k: int = _RRF_K) -> list[Hit]:
    scores: dict[tuple[str, int], float] = {}
    keep: dict[tuple[str, int], Hit] = {}
    for lst in rank_lists:
        for rank, h in enumerate(lst):
            key = (h.owner_table, h.owner_id)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            keep[key] = h
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    out = []
    for (ot, oid), s in ordered:
        h = keep[(ot, oid)]
        out.append(Hit(h.owner_table, h.owner_id, h.body, s, h.session_id, h.extra))
    return out


class Router:
    def __init__(self, store: Store, backend: EmbeddingBackend | None = None) -> None:
        self.store = store
        self.backend = backend

    @property
    def vectors_enabled(self) -> bool:
        return self.backend is not None

    def recall(self, query: str, scope: str = "all", session_id: str = "",
               limit: int = 10) -> list[Hit]:
        lists: list[list[Hit]] = []
        fts_hits = self.store.fts_search(query, scope=scope, session_id=session_id,
                                         limit=limit * 2)
        if fts_hits:
            lists.append(fts_hits)
        if self.backend is not None:
            qv = self.backend.embed_query(query)
            tables = {"all": None, "session": ["um_messages", "um_summaries"],
                      "facts": ["um_facts"]}.get(scope)
            scored = []
            for ot, oid, vec in self.store.all_vectors(tables):
                if len(vec) != len(qv):
                    continue  # чужой dim — пропускаем, не падаем
                body, sid = self.store._body_of(ot, oid)
                if body is None:
                    continue
                if scope == "session" and ot == "um_messages" and sid != session_id:
                    continue
                scored.append(Hit(ot, oid, body, cosine(qv, vec), sid))
            scored.sort(key=lambda h: -h.score)
            if scored:
                lists.append(scored[:limit * 2])
        if not lists:
            return []
        if len(lists) == 1:
            return lists[0][:limit]
        return rrf_fuse(lists)[:limit]
