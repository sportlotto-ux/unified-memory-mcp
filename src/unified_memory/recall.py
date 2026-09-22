"""Recall router: FTS + vectors + RRF fusion.

Один recall вместо трёх (lcm_recall / lcm_grep / mnemosyne_recall).
Без векторов (fastembed не стоит) — чистый FTS, флаг виден в mem_status.
"""

from __future__ import annotations

from .embeddings import EmbeddingBackend
from .store import Hit, Store, cosine, tokenize

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


VALID_SCOPES = ("all", "session", "facts")


class Router:
    def __init__(self, store: Store, backend: EmbeddingBackend | None = None) -> None:
        self.store = store
        self.backend = backend
        self.last_stats: dict = {}

    @property
    def vectors_enabled(self) -> bool:
        return self.backend is not None

    def recall(self, query: str, scope: str = "all", session_id: str = "",
               limit: int = 10) -> list[Hit]:
        if scope not in VALID_SCOPES:
            raise ValueError(f"unknown scope {scope!r}: {VALID_SCOPES}")
        if limit <= 0:
            return []
        if scope == "session" and not session_id:
            return []  # #5: без session_id граф/поиск вернули бы чужие данные
        self.last_stats = {"dim_skipped": 0}
        lists: list[list[Hit]] = []
        fts_hits = self.store.fts_search(query, scope=scope, session_id=session_id,
                                         limit=limit * 2)
        if fts_hits:
            lists.append(fts_hits)
        if self.backend is not None:
            qv = self.backend.embed_query(query)
            tables = {"all": None, "session": ["um_messages", "um_summaries", "um_edges"],
                      "facts": ["um_facts"]}[scope]
            # Batch: все вектора одним проходом, тела — bodies_for (макс. 4 запроса).
            all_vecs = self.store.all_vectors(tables)
            cand = [(ot, oid, vec) for ot, oid, vec in all_vecs
                    if len(vec) == len(qv)]
            self.last_stats["dim_skipped"] = len(all_vecs) - len(cand)
            bodies = self.store.bodies_for([(ot, oid) for ot, oid, _ in cand])
            scored = []
            for ot, oid, vec in cand:
                found = bodies.get((ot, oid))
                if not found or found[0] is None:
                    continue
                body, sid = found
                if scope == "session" and ot in ("um_messages", "um_edges", "um_summaries") \
                        and sid != session_id:
                    continue
                scored.append(Hit(ot, oid, body, cosine(qv, vec), sid))
            scored.sort(key=lambda h: -h.score)
            if scored:
                lists.append(scored[:limit * 2])
        graph_hits = self._graph_arm(query, scope, session_id, limit * 2)
        if graph_hits:
            lists.append(graph_hits)
        if not lists:
            return []
        if len(lists) == 1:
            return lists[0][:limit]
        return rrf_fuse(lists)[:limit]

    def _graph_arm(self, query: str, scope: str, session_id: str,
                   limit: int) -> list[Hit]:
        """1-hop expansion: совпавшие сущности -> их рёбра."""
        if scope == "facts":
            return []
        terms = tokenize(query)
        if not terms:
            return []
        hits: list[Hit] = []
        seen: set[int] = set()
        for ent in self.store.match_entities(terms, limit=5):
            for nb in self.store.neighbors(
                    ent, session_id if scope == "session" else ""):
                eid = nb["edge_id"]
                if eid in seen:
                    continue
                seen.add(eid)
                body = f"{nb['subject']} --{nb['predicate']}--> {nb['object']}"
                hits.append(Hit("um_edges", eid, body, 1.0, nb["session_id"]))
                if len(hits) >= limit:
                    return hits
        return hits
