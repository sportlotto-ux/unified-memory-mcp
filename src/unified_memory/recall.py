"""Recall router: FTS + vectors + RRF fusion.

Один recall вместо трёх (lcm_recall / lcm_grep / mnemosyne_recall).
Без векторов (fastembed не стоит) — чистый FTS, флаг виден в mem_status.
"""

from __future__ import annotations

import math
import time

from .config import Config
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
    def __init__(self, store: Store, backend: EmbeddingBackend | None = None,
                 cfg: Config | None = None) -> None:
        self.store = store
        self.backend = backend
        self.cfg = cfg or Config()
        self.last_stats: dict = {}

    @property
    def vectors_enabled(self) -> bool:
        return self.backend is not None

    def recall(self, query: str, scope: str = "all", session_id: str = "",
               limit: int = 10, owner: str = "",
               include_expired: bool = False,
               as_of: float | None = None,
               hops: int = 1, rel: str = "") -> list[Hit]:
        if scope not in VALID_SCOPES:
            raise ValueError(f"unknown scope {scope!r}: {VALID_SCOPES}")
        if limit <= 0:
            return []
        if scope == "session" and not session_id:
            return []  # #5: без session_id граф/поиск вернули бы чужие данные
        self.last_stats = {"dim_skipped": 0}
        lists: list[list[Hit]] = []
        fts_hits = self.store.fts_search(query, scope=scope, session_id=session_id,
                                         limit=limit * 2, owner=owner,
                                         include_expired=include_expired, as_of=as_of)
        if fts_hits:
            lists.append(fts_hits)
        if self.backend is not None:
            qv = self.backend.embed_query(query)
            tables = {"all": None, "session": ["um_messages", "um_summaries", "um_edges"],
                      "facts": ["um_facts"]}[scope]
            # v0.4-п.6: KNN-кандидаты из vec0 + ТОЧНЫЙ косинусный перескоринг
            # (порядок L2 == порядку косинуса на нормализованных векторах;
            # шкала оценок не меняется — паритет с brute force).
            cand = []
            self.last_stats["vec_index"] = "brute"
            if self.cfg.vec_index != "off":
                knn_rows = self.store.knn(qv, tables, owner, limit * 2)
                if knn_rows:
                    vecs = self.store.vectors_for(
                        [(ot, oid) for ot, oid, _ in knn_rows])
                    cand = [(ot, oid, vecs[(ot, oid)]) for ot, oid, _ in knn_rows
                            if (ot, oid) in vecs and len(vecs[(ot, oid)]) == len(qv)]
                    if cand:
                        self.last_stats["vec_index"] = "knn"
            if not cand:
                # Batch: все вектора одним проходом (фолбэк без индекса).
                all_vecs = self.store.all_vectors(tables, owner)
                cand = [(ot, oid, vec) for ot, oid, vec in all_vecs
                        if len(vec) == len(qv)]
                self.last_stats["dim_skipped"] = len(all_vecs) - len(cand)
            if cand and as_of is not None:
                win = self.store.window_for([(ot, oid) for ot, oid, _ in cand])
                cand = [(ot, oid, v) for ot, oid, v in cand
                        if (lambda w: w is None or
                            (w[0] <= as_of and (w[1] == 0 or w[1] > as_of)))(
                                win.get((ot, oid)))]
            elif cand and not include_expired:
                now = time.time()
                val = self.store.validity_for([(ot, oid) for ot, oid, _ in cand])
                cand = [(ot, oid, v) for ot, oid, v in cand
                        if val.get((ot, oid), 0.0) == 0.0
                        or val.get((ot, oid), 0.0) > now]
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
        if hops > 1:
            # guardrail 1: старый _graph_arm не тронут; BFS — отдельная ветка.
            seeds = [(h.owner_table, h.owner_id) for lst in lists for h in lst]
            graph_hits = self._graph_bfs(query, scope, session_id, limit * 2,
                                         owner, include_expired, as_of, hops,
                                         rel, seeds)
        else:
            graph_hits = self._graph_arm(query, scope, session_id, limit * 2, owner,
                                         include_expired, as_of)
        if graph_hits:
            lists.append(graph_hits)
        if not lists:
            return []
        # v0.4-п.3: один пайп поверх fused — recency-приор, scope-bias, MMR.
        # Timestamps одним batch (макс. 4 запроса), а не правками трёх arm'ов.
        fused = lists[0] if len(lists) == 1 else rrf_fuse(lists)
        stamps = self.store.created_for([(h.owner_table, h.owner_id) for h in fused])
        now = time.time()
        adjusted = []
        for h in fused:
            score = h.score
            ts = stamps.get((h.owner_table, h.owner_id), 0.0)
            if self.cfg.recency_halflife_days > 0 and ts > 0:
                age_days = max(0.0, (now - ts) / 86400.0)
                score *= 0.5 + 0.5 * math.exp(-age_days / self.cfg.recency_halflife_days)
            if scope == "all" and session_id and h.session_id == session_id:
                score *= 1.0 + self.cfg.scope_bias
            adjusted.append(Hit(h.owner_table, h.owner_id, h.body, score,
                                h.session_id, h.extra, ts))
        adjusted.sort(key=lambda h: -h.score)
        self.last_stats["reranked"] = len(adjusted)
        return self._mmr(adjusted, limit)

    def _mmr(self, hits: list[Hit], limit: int) -> list[Hit]:
        """MMR по Жаккару токенов тел: дубли parent/children не забивают топ.

        λ=1 — чистый relevance-порядок без перебора пар. Оценки нормируем
        на max (arm'ы живут в разных шкалах: FTS=1.0, cosine∈[-1,1], RRF≈0.01).
        """
        if self.cfg.mmr_lambda >= 1.0 or len(hits) <= 1:
            return hits[:limit]
        cands = hits[:limit * 2]
        peak = max((h.score for h in cands), default=0.0) or 1.0
        sets = [set(tokenize(h.body)) for h in cands]
        picked = [0]
        chosen = {0}
        lam = self.cfg.mmr_lambda
        while len(picked) < min(limit, len(cands)):
            best, best_val = -1, float("-inf")
            for i in range(1, len(cands)):
                if i in chosen:
                    continue
                sim = max((len(sets[i] & sets[j]) / max(1, len(sets[i] | sets[j]))
                           for j in picked), default=0.0)
                val = lam * (cands[i].score / peak) - (1.0 - lam) * sim
                if val > best_val:
                    best, best_val = i, val
            if best < 0:
                break
            picked.append(best)
            chosen.add(best)
        return [cands[i] for i in picked]

    def _graph_arm(self, query: str, scope: str, session_id: str,
                   limit: int, owner: str = "",
                   include_expired: bool = False,
                   as_of: float | None = None) -> list[Hit]:
        """1-hop expansion: совпавшие сущности -> их рёбра (срез на as_of)."""
        if scope == "facts":
            return []
        terms = tokenize(query)
        if not terms:
            return []
        hits: list[Hit] = []
        seen: set[int] = set()
        for ent in self.store.match_entities(terms, limit=5, owner=owner):
            for nb in self.store.neighbors(
                    ent, session_id if scope == "session" else "", owner=owner,
                    include_expired=include_expired, as_of=as_of):
                eid = nb["edge_id"]
                if eid in seen:
                    continue
                seen.add(eid)
                body = f"{nb['subject']} --{nb['predicate']}--> {nb['object']}"
                hits.append(Hit("um_edges", eid, body, 1.0, nb["session_id"]))
                if len(hits) >= limit:
                    return hits
        return hits

    def _graph_bfs(self, query: str, scope: str, session_id: str, limit: int,
                   owner: str, include_expired: bool, as_of: float | None,
                   hops: int, rel: str,
                   seeds: list[tuple[str, int]]) -> list[Hit]:
        """BFS по um_links ∪ um_edges для hops>1 (ADR-001 D5, guardrails п.4).

        Узел = (table, id); сущности traversal-only. Скоринг decay**(depth-1):
        hop=1 → 1.0 (как старый _graph_arm), hop=2 → decay. Fan-out и общий
        потолок limit — остановка. visited-set против циклов A→B→A.
        """
        max_hops = max(1, min(int(hops), self.cfg.recall_max_hops))
        decay = self.cfg.graph_decay
        fanout = self.cfg.link_fanout
        filt = (rel or "").strip().lower()
        sess = session_id if scope == "session" else ""
        ent = "um_entities"
        content = ("um_messages", "um_facts", "um_summaries", "um_edges")
        seed_keys = {(t, i) for t, i in seeds}
        visited: set[tuple[str, int]] = set()
        node_cap = max(limit * fanout, 100)  # страховка от dense-взрыва
        queue: list[tuple[str, int, int]] = []
        emitted: dict[tuple[str, int], tuple[float, str, str]] = {}

        def enqueue(table: str, oid: int, depth: int) -> None:
            key = (table, oid)
            if key not in visited and len(visited) < node_cap:
                visited.add(key)
                queue.append((table, oid, depth))

        for t, i in seeds:
            if t in content:
                enqueue(t, i, 0)
        if scope != "facts":
            for eid in self.store.match_entity_ids(tokenize(query), limit=5,
                                                   owner=owner):
                enqueue(ent, eid, 0)

        head = 0
        while head < len(queue):
            table, oid, depth = queue[head]
            head += 1
            if depth >= max_hops:
                continue
            nd = depth + 1
            score = decay ** (nd - 1)  # hop=1 → 1.0
            if table == ent:
                for st in self.store.edge_steps_of_entity(
                        oid, owner=owner, include_expired=include_expired,
                        as_of=as_of, session_id=sess, predicate=filt, limit=100):
                    ekey = ("um_edges", st["edge_id"])
                    if ekey not in emitted and ekey not in seed_keys:
                        body, msid = self.store._body_of("um_edges", st["edge_id"])
                        if body is not None:
                            emitted[ekey] = (score, body, msid or "")
                    enqueue("um_edges", st["edge_id"], nd)
                    enqueue(ent, st["other_id"], nd)
                    if len(emitted) >= limit:
                        break
            else:
                if table == "um_facts":  # мост fact→entities (traversal-only)
                    for br in self.store.entity_ids_for_fact(oid, owner=owner):
                        enqueue(ent, br["subject_id"], nd)
                        enqueue(ent, br["object_id"], nd)
                self._bfs_links(table, oid, nd, score, owner, include_expired,
                                as_of, filt, sess, fanout, seed_keys, emitted,
                                enqueue, limit)
            if len(emitted) >= limit:
                break

        hits = [Hit(k[0], k[1], v[1], v[0], v[2]) for k, v in emitted.items()]
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]

    def _bfs_links(self, table: str, oid: int, nd: int, score: float,
                   owner: str, include_expired: bool, as_of: float | None,
                   filt: str, sess: str, fanout: int,
                   seed_keys: set, emitted: dict, enqueue, limit: int) -> None:
        """Расширение узла по um_links: liveness линка (в link_neighbors) +
        liveness узла-назначения (node_ok) — guardrail 4."""
        for nb in self.store.link_neighbors(
                table, oid, owner=owner, include_expired=include_expired,
                as_of=as_of, rel=filt, session_id=sess, limit=fanout):
            nt, nid = nb["table"], nb["id"]
            key = (nt, nid)
            if key not in emitted and key not in seed_keys \
                    and self.store.node_ok(nt, nid, owner, include_expired, as_of):
                found = self.store.bodies_for([(nt, nid)]).get((nt, nid))
                if found and found[0] is not None:
                    emitted[key] = (score, found[0], found[1])
            enqueue(nt, nid, nd)
            if len(emitted) >= limit:
                return
