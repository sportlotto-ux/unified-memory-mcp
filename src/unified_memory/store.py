"""Unified store: one SQLite for messages, summaries, facts, vectors.

v0.1: синхронный sqlite3, WAL, single-writer через короткий busy_timeout.
Векторный поиск — brute-force cosine поверх BLOB (float32 LE); путь
масштабирования — sqlite-vec, интерфейс не меняется.
"""

from __future__ import annotations

import re
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .embeddings import check_store_dim

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS um_messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'unknown',
    externalized_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_um_messages_session ON um_messages(session_id, id);

CREATE TABLE IF NOT EXISTS um_summaries (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,
    covers_from INTEGER,
    covers_to INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_um_summaries_session ON um_summaries(session_id, depth);

CREATE TABLE IF NOT EXISTS um_facts (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_um_facts_cat ON um_facts(category);

CREATE TABLE IF NOT EXISTS um_vectors (
    id INTEGER PRIMARY KEY,
    owner_table TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    model TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_um_vectors_owner ON um_vectors(owner_table, owner_id);

CREATE TABLE IF NOT EXISTS um_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Граф памяти (источник: mnemosyne triples/episodic_graph).
-- Сущности канонизируются по lower().strip(); вектора — в um_vectors.
CREATE TABLE IF NOT EXISTS um_entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS um_edges (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES um_entities(id),
    predicate TEXT NOT NULL,
    object_id INTEGER NOT NULL REFERENCES um_entities(id),
    session_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_um_edges_subj ON um_edges(subject_id);
CREATE INDEX IF NOT EXISTS idx_um_edges_obj ON um_edges(object_id);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS um_fts USING fts5(
    owner_table, owner_id UNINDEXED, body, tokenize='trigram'
);
"""

_WORD_RE = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text)]


def pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _locked(fn):
    import functools

    @functools.wraps(fn)
    def w(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)

    return w


@dataclass
class Hit:
    owner_table: str
    owner_id: int
    body: str
    score: float
    session_id: str = ""
    extra: str = ""


class Store:
    """Синхронное ядро. Один инстанс на процесс (см. single-writer в MIGRATION_PLAN)."""

    def __init__(self, cfg: Config, embedding_dim: int = 0, embedding_model: str = "") -> None:
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(cfg.db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)
        try:
            self.conn.executescript(_FTS_SCHEMA)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False  # сборка без FTS5 — деградация до LIKE, флаг в mem_status
        if embedding_dim:
            check_store_dim(embedding_dim, embedding_model, self.meta_get)
            if self.meta_get("embedding_model") is None:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO um_meta(key, value) VALUES(?, ?)",
                    [("embedding_model", embedding_model),
                     ("embedding_dim", str(embedding_dim)),
                     ("schema_version", "1")],
                )
                self.conn.commit()

    # -- meta -------------------------------------------------------------
    @_locked
    def meta_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM um_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # -- writes -----------------------------------------------------------
    @_locked
    def add_message(self, session_id: str, role: str, content: str,
                    source: str = "unknown") -> int:
        cur = self.conn.execute(
            "INSERT INTO um_messages(session_id, role, content, created_at, source)"
            " VALUES(?,?,?,?,?)",
            (session_id, role, content, time.time(), source),
        )
        mid = cur.lastrowid
        self._fts_index("um_messages", mid, content)
        self.conn.commit()
        return mid

    @_locked
    def add_fact(self, category: str, name: str, body: str,
                 importance: float = 0.5) -> int:
        importance = max(0.0, min(1.0, importance))  # кап вместо жёстких 0.95
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO um_facts(category, name, body, importance, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (category, name, body, importance, now, now),
        )
        fid = cur.lastrowid
        self._fts_index("um_facts", fid, f"{name} {body}")
        self.conn.commit()
        return fid

    @_locked
    def add_summary(self, session_id: str, body: str, depth: int = 0,
                    covers_from: int | None = None, covers_to: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO um_summaries(session_id, depth, body, covers_from, covers_to, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (session_id, depth, body, covers_from, covers_to, time.time()),
        )
        sid = cur.lastrowid
        self._fts_index("um_summaries", sid, body)
        self.conn.commit()
        return sid

    @_locked
    def add_vector(self, owner_table: str, owner_id: int,
                   vec: list[float], model: str) -> None:
        self.conn.execute(
            "INSERT INTO um_vectors(owner_table, owner_id, embedding, model)"
            " VALUES(?,?,?,?)",
            (owner_table, owner_id, pack_vector(vec), model),
        )
        self.conn.commit()

    @_locked
    def _fts_index(self, owner_table: str, owner_id: int, body: str) -> None:
        if self.fts:
            self.conn.execute(
                "INSERT INTO um_fts(owner_table, owner_id, body) VALUES(?,?,?)",
                (owner_table, owner_id, body),
            )

    # -- reads ------------------------------------------------------------
    @_locked
    def get_message(self, mid: int) -> dict | None:
        row = self.conn.execute(
            "SELECT id, session_id, role, content, created_at, source"
            " FROM um_messages WHERE id=?", (mid,)).fetchone()
        if not row:
            return None
        return dict(zip(["id", "session_id", "role", "content", "created_at", "source"], row))

    @_locked
    def session_messages(self, session_id: str, after_id: int = 0,
                         limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, session_id, role, content, created_at, source FROM um_messages"
            " WHERE session_id=? AND id>? ORDER BY id LIMIT ?",
            (session_id, after_id, limit)).fetchall()
        keys = ["id", "session_id", "role", "content", "created_at", "source"]
        return [dict(zip(keys, r)) for r in rows]

    @_locked
    def fts_search(self, query: str, scope: str = "all",
                   session_id: str = "", limit: int = 20) -> list[Hit]:
        """Полнотекст: FTS5 при наличии, иначе LIKE по токенам."""
        terms = tokenize(query)
        if not terms:
            return []
        tables = {"all": ["um_messages", "um_summaries", "um_facts"],
                  "session": ["um_messages", "um_summaries"],
                  "facts": ["um_facts"]}.get(scope, ["um_messages", "um_summaries", "um_facts"])
        if self.fts:
            match = " OR ".join(f'"{t}"' for t in terms[:10])
            q = ("SELECT owner_table, owner_id FROM um_fts WHERE um_fts MATCH ? LIMIT ?")
            try:
                rows = self.conn.execute(q, (match, limit * 3)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            hits = []
            for ot, oid in rows:
                if ot not in tables:
                    continue
                body, sid = self._body_of(ot, oid)
                if body is None:
                    continue
                if scope == "session" and ot == "um_messages" and sid != session_id:
                    continue
                hits.append(Hit(ot, oid, body, 1.0, sid))
                if len(hits) >= limit:
                    break
            return hits
        # LIKE fallback
        hits = []
        for ot in tables:
            col = "content" if ot == "um_messages" else "body"
            cond = " OR ".join([f"{col} LIKE ?"] * len(terms[:6]))
            params: list = [f"%{t}%" for t in terms[:6]]
            if ot == "um_messages" and scope == "session":
                cond = f"(session_id=?) AND ({cond})"
                params = [session_id] + params
            idcol = "id"
            for r in self.conn.execute(
                    f"SELECT {idcol}, {col} FROM {ot} WHERE {cond} LIMIT ?", (*params, limit)):
                oid, body = r
                sid = ""
                if ot == "um_messages":
                    s = self.conn.execute(
                        "SELECT session_id FROM um_messages WHERE id=?", (oid,)).fetchone()
                    sid = s[0] if s else ""
                hits.append(Hit(ot, oid, body, 1.0, sid))
        return hits[:limit]

    @_locked
    def _body_of(self, owner_table: str, owner_id: int) -> tuple[str | None, str]:
        if owner_table == "um_messages":
            r = self.conn.execute(
                "SELECT content, session_id FROM um_messages WHERE id=?", (owner_id,)).fetchone()
            return (r[0], r[1]) if r else (None, "")
        if owner_table == "um_summaries":
            r = self.conn.execute(
                "SELECT body FROM um_summaries WHERE id=?", (owner_id,)).fetchone()
            return ((r[0], "") if r else (None, ""))
        if owner_table == "um_edges":
            r = self.conn.execute(
                """SELECT s.name, e.predicate, o.name FROM um_edges e
                   JOIN um_entities s ON s.id = e.subject_id
                   JOIN um_entities o ON o.id = e.object_id
                   WHERE e.id=?""", (owner_id,)).fetchone()
            return ((f"{r[0]} --{r[1]}--> {r[2]}", "") if r else (None, ""))
        if owner_table == "um_facts":
            r = self.conn.execute(
                "SELECT name, body FROM um_facts WHERE id=?", (owner_id,)).fetchone()
            return ((f"{r[0]}: {r[1]}", "") if r else (None, ""))
        return None, ""

    @_locked
    def all_vectors(self, owner_tables: list[str] | None = None) -> list[tuple[str, int, list[float]]]:
        q = "SELECT owner_table, owner_id, embedding FROM um_vectors"
        params: list = []
        if owner_tables:
            q += f" WHERE owner_table IN ({','.join('?' * len(owner_tables))})"
            params = owner_tables
        return [(ot, oid, unpack_vector(b)) for ot, oid, b in self.conn.execute(q, params)]

    @_locked
    def delete_fact(self, fid: int) -> bool:
        cur = self.conn.execute("DELETE FROM um_facts WHERE id=?", (fid,))
        self.conn.execute("DELETE FROM um_vectors WHERE owner_table='um_facts' AND owner_id=?", (fid,))
        if self.fts:
            self.conn.execute(
                "DELETE FROM um_fts WHERE owner_table='um_facts' AND owner_id=?", (fid,))
        self.conn.commit()
        return cur.rowcount > 0

    @_locked

    # -- graph ----------------------------------------------------------
    @_locked
    def add_entity(self, name: str) -> int:
        import time as _t
        key = name.strip().lower()
        row = self.conn.execute(
            "SELECT id FROM um_entities WHERE name=?", (key,)).fetchone()
        if row:
            return row[0]
        cur = self.conn.execute(
            "INSERT INTO um_entities(name, created_at) VALUES(?,?)", (key, _t.time()))
        self.conn.commit()
        return cur.lastrowid

    @_locked
    def add_edge(self, subject: str, predicate: str, obj: str,
                 session_id: str = "") -> int:
        import time as _t
        sid = self.add_entity(subject)
        oid = self.add_entity(obj)
        cur = self.conn.execute(
            "INSERT INTO um_edges(subject_id, predicate, object_id, session_id, created_at)"
            " VALUES(?,?,?,?,?)",
            (sid, predicate.strip().lower(), oid, session_id, _t.time()))
        eid = cur.lastrowid
        self._fts_index("um_edges", eid, f"{subject} {predicate} {obj}")
        self.conn.commit()
        return eid

    @_locked
    def neighbors(self, entity_name: str) -> list[dict]:
        key = entity_name.strip().lower()
        row = self.conn.execute(
            "SELECT id FROM um_entities WHERE name=?", (key,)).fetchone()
        if not row:
            return []
        eid = row[0]
        rows = self.conn.execute(
            """SELECT e.id, s.name, e.predicate, o.name, e.session_id
               FROM um_edges e
               JOIN um_entities s ON s.id = e.subject_id
               JOIN um_entities o ON o.id = e.object_id
               WHERE e.subject_id = ? OR e.object_id = ?""", (eid, eid)).fetchall()
        return [dict(zip(["edge_id", "subject", "predicate", "object", "session_id"], r))
                for r in rows]

    @_locked
    def match_entities(self, terms: list[str], limit: int = 10) -> list[str]:
        out = []
        for t in terms[:8]:
            for r in self.conn.execute(
                    "SELECT name FROM um_entities WHERE name LIKE ? LIMIT ?",
                    (f"%{t}%", limit)):
                if r[0] not in out:
                    out.append(r[0])
        return out[:limit]

    def stats(self) -> dict:
        out = {}
        for t in ["um_messages", "um_summaries", "um_facts", "um_vectors",
           "um_entities", "um_edges"]:
            out[t] = self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        out["fts"] = self.fts
        out["embedding_model"] = self.meta_get("embedding_model")
        return out

    @_locked
    def close(self) -> None:
        self.conn.close()


def open_db(cfg: Config, embedding_dim: int, embedding_model: str):
    """Совместимость со stage-0 API: возвращает Store."""
    return Store(cfg, embedding_dim, embedding_model)
