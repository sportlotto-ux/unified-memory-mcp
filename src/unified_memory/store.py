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
    owner TEXT NOT NULL DEFAULT '',  -- v0.4-п.2: тенант; '' = legacy без изоляции
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'unknown',
    externalized_ref TEXT
);
-- Индексы создаются кодом (_INDEXES) ПОСЛЕ миграций: на legacy-БД
-- колонки owner ещё нет, и CREATE INDEX падал бы с no such column.

CREATE TABLE IF NOT EXISTS um_summaries (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    depth INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,
    covers_from INTEGER,
    covers_to INTEGER,
    superseded_by INTEGER NOT NULL DEFAULT 0,  -- #2: схлопнуто в ноду ( lineage живёт)
    created_at REAL NOT NULL
);


CREATE TABLE IF NOT EXISTS um_facts (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    -- Слот-семантика (v0.5): 0 = живое (sentinel!), иначе timestamp истечения.
    -- NOT NULL обязателен: NULL-строки выпали бы из WHERE valid_until=0
    -- и обошли бы partial unique index ниже.
    valid_until REAL NOT NULL DEFAULT 0,
    superseded_by INTEGER NOT NULL DEFAULT 0  -- цепочка версий, как у саммари
);


CREATE TABLE IF NOT EXISTS um_vectors (
    id INTEGER PRIMARY KEY,
    owner_table TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    model TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT ''  -- денормализация: фильтр без джойнов
);
CREATE TABLE IF NOT EXISTS um_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Граф памяти (источник: mnemosyne triples/episodic_graph).
-- Сущности канонизируются по lower().strip(); вектора — в um_vectors.
CREATE TABLE IF NOT EXISTS um_entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,   -- каноническое: lower().strip()
    display TEXT NOT NULL DEFAULT '',  -- исходное написание («Иван», не «иван»)
    created_at REAL NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    UNIQUE(name, owner)  -- одно имя — разные тенанты, без пересечений
);
CREATE TABLE IF NOT EXISTS um_edges (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES um_entities(id),
    predicate TEXT NOT NULL,
    object_id INTEGER NOT NULL REFERENCES um_entities(id),
    session_id TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    fact_id INTEGER NOT NULL DEFAULT 0,  -- provenance: какой mem_fact породил
    created_at REAL NOT NULL,
    valid_until REAL NOT NULL DEFAULT 0  -- 0 = живое; замена ребра = новое ребро
);
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_um_messages_session ON um_messages(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_um_messages_owner ON um_messages(owner, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_summaries_session ON um_summaries(session_id, depth)",
    "CREATE INDEX IF NOT EXISTS idx_um_summaries_owner ON um_summaries(owner, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_facts_cat ON um_facts(category)",
    "CREATE INDEX IF NOT EXISTS idx_um_facts_owner ON um_facts(owner)",
    "CREATE INDEX IF NOT EXISTS idx_um_vectors_owner ON um_vectors(owner_table, owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_vectors_tenant ON um_vectors(owner)",
    "CREATE INDEX IF NOT EXISTS idx_um_vectors_model ON um_vectors(model)",
    "CREATE INDEX IF NOT EXISTS idx_um_edges_subj ON um_edges(subject_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_edges_obj ON um_edges(object_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_edges_fact ON um_edges(fact_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_edges_session ON um_edges(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_edges_owner ON um_edges(owner, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_entities_owner ON um_entities(owner)",
    # v0.5: один живой факт на слот (owner, category, name). Partial по sentinel 0.
    # Создаётся ПОСЛЕ миграции и dedupe — иначе падает на грязной БД.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_um_facts_live"
    " ON um_facts(owner, category, name) WHERE valid_until = 0",
]

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


_VEC_MOD = {"mod": None, "tried": False}

# Таблицы, чьи вектора индексируем (um_entities — тоже: граф-arm идёт через вектора).
_VEC_TABLES = ("um_messages", "um_summaries", "um_facts", "um_edges", "um_entities")


def vec_extension_available() -> bool:
    """sqlite-vec importable? Только импорт (дешёвый); load — в _vec_ensure."""
    if not _VEC_MOD["tried"]:
        _VEC_MOD["tried"] = True
        try:
            import sqlite_vec

            _VEC_MOD["mod"] = sqlite_vec
        except ImportError:
            _VEC_MOD["mod"] = None
    return _VEC_MOD["mod"] is not None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


_TIKTOKEN = {"enc": None, "tried": False}


def _tiktoken_enc():
    if not _TIKTOKEN["tried"]:
        _TIKTOKEN["tried"] = True
        try:
            import tiktoken

            _TIKTOKEN["enc"] = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _TIKTOKEN["enc"] = None
    return _TIKTOKEN["enc"]


def estimate_tokens(text: str) -> int:
    enc = _tiktoken_enc()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, len(text) // 4)


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
    created_at: float = 0.0  # v0.4-п.3: штампует Router для recency-приора


class Store:
    """Синхронное ядро. Один инстанс на процесс (см. single-writer в MIGRATION_PLAN)."""

    def __init__(self, cfg: Config, embedding_dim: int = 0, embedding_model: str = "") -> None:
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(cfg.db_path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(cfg.db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)
        # Миграция существующих БД: fact_id добавлен позже (#4).
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(um_edges)")]
        if "fact_id" not in cols:
            self.conn.execute("ALTER TABLE um_edges ADD COLUMN fact_id INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        scols = [r[1] for r in self.conn.execute("PRAGMA table_info(um_summaries)")]
        if "superseded_by" not in scols:
            self.conn.execute(
                "ALTER TABLE um_summaries ADD COLUMN superseded_by INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        ecols = [r[1] for r in self.conn.execute("PRAGMA table_info(um_entities)")]
        if "display" not in ecols:
            self.conn.execute("ALTER TABLE um_entities ADD COLUMN display TEXT NOT NULL DEFAULT ''")
            self.conn.execute("UPDATE um_entities SET display=name WHERE display=''")
            self.conn.commit()
        # v0.4-п.2: owner-колонки. '' = legacy без изоляции, поведение не меняется.
        for t in ("um_messages", "um_summaries", "um_facts",
                  "um_edges", "um_vectors"):
            cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({t})")]
            if "owner" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {t} ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
                self.conn.commit()
        ecols = [r[1] for r in self.conn.execute("PRAGMA table_info(um_entities)")]
        if "owner" not in ecols:
            # UNIQUE(name) -> UNIQUE(name, owner): только через пересборку.
            self.conn.executescript("""
                CREATE TABLE um_entities_new(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    display TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                    owner TEXT NOT NULL DEFAULT '', UNIQUE(name, owner));
                INSERT INTO um_entities_new(id, name, display, created_at, owner)
                    SELECT id, name, display, created_at, '' FROM um_entities;
                DROP TABLE um_entities;
                ALTER TABLE um_entities_new RENAME TO um_entities;
            """)
            self.conn.commit()
        # v0.5: valid_until (sentinel 0 = живое) + superseded_by на фактах.
        fcols = [r[1] for r in self.conn.execute("PRAGMA table_info(um_facts)")]
        for col, ddl in (("valid_until", "REAL NOT NULL DEFAULT 0"),
                         ("superseded_by", "INTEGER NOT NULL DEFAULT 0")):
            if col not in fcols:
                self.conn.execute(f"ALTER TABLE um_facts ADD COLUMN {col} {ddl}")
                self.conn.commit()
        ecols2 = [r[1] for r in self.conn.execute("PRAGMA table_info(um_edges)")]
        if "valid_until" not in ecols2:
            self.conn.execute(
                "ALTER TABLE um_edges ADD COLUMN valid_until REAL NOT NULL DEFAULT 0")
            self.conn.commit()
        self._dedupe_live_slots()
        # Индексы строго после миграций: на legacy-таблицах колонок ещё нет.
        for stmt in _INDEXES:
            self.conn.execute(stmt)
        self.conn.commit()
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

    @_locked
    def select(self, sql: str, params: tuple = ()) -> list[tuple]:
        """#6: чтение из движка только через лок."""
        return self.conn.execute(sql, params).fetchall()

    @_locked
    def execute_write(self, sql: str, params: tuple = ()) -> int:
        """#6: запись из движка только через лок. Возвращает rowcount."""
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.rowcount

    @_locked
    def bump_tokens(self, session_id: str, delta: int, owner: str = "") -> None:
        """#3: инкрементный счётчик давления. Вызывать из locked-контекста."""
        key = f"tokens:{owner}:{session_id}" if owner else f"tokens:{session_id}"
        self.conn.execute(
            "INSERT INTO um_meta(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE"
            " SET value=CAST(value AS INTEGER)+CAST(excluded.value AS INTEGER)",
            (key, str(delta)))
        self.conn.commit()

    @_locked
    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO um_meta(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    # -- writes -----------------------------------------------------------
    @_locked
    def add_message(self, session_id: str, role: str, content: str,
                    source: str = "unknown", owner: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO um_messages(session_id, owner, role, content, created_at, source)"
            " VALUES(?,?,?,?,?,?)",
            (session_id, owner, role, content, time.time(), source),
        )
        mid = cur.lastrowid
        self._fts_index("um_messages", mid, content)
        self.bump_tokens(session_id, estimate_tokens(content), owner)
        self.conn.commit()
        return mid

    @_locked
    def add_fact(self, category: str, name: str, body: str,
                 importance: float = 0.5, owner: str = "") -> int:
        """Слот-запись: то же тело — no-op, новое — supersede-цепочка. Возвращает id живого."""
        return self._upsert_fact(category, name, body, importance, owner)["id"]

    @_locked
    def add_fact_ex(self, category: str, name: str, body: str,
                    importance: float = 0.5, owner: str = "") -> dict:
        """Слот-запись с полным статусом: {id, status, superseded_id}."""
        return self._upsert_fact(category, name, body, importance, owner)

    def _upsert_fact(self, category: str, name: str, body: str,
                     importance: float, owner: str = "",
                     _depth: int = 0) -> dict:
        """Ядро слот-семантики (v0.5). Без лока — из locked-контекста.

        Живой слот = (owner, category, name, valid_until=0); partial unique index
        делает «два живых» невозможными на уровне БД. Expire-then-insert
        атомарен внутри @_locked, IntegrityError — только backstop на гонку.
        """
        importance = max(0.0, min(1.0, importance))  # кап вместо жёстких 0.95
        now = time.time()
        row = self.conn.execute(
            "SELECT id, body, importance FROM um_facts WHERE owner=? AND category=?"
            " AND name=? AND valid_until=0", (owner, category, name)).fetchone()
        if row is not None:
            fid, old_body, old_imp = row
            if old_body == body:
                if abs(old_imp - importance) > 1e-9:
                    self.conn.execute(
                        "UPDATE um_facts SET importance=?, updated_at=? WHERE id=?",
                        (importance, now, fid))
                    self.conn.commit()
                    return {"id": fid, "status": "updated", "superseded_id": 0}
                return {"id": fid, "status": "noop", "superseded_id": 0}
            # Expire-then-insert: partial unique index не пустит новый живой
            # факт, пока старой живёт. При сбое вставки — rollback вернёт старую.
            try:
                self.conn.execute(
                    "UPDATE um_facts SET valid_until=?, updated_at=? WHERE id=?",
                    (now, now, fid))
                cur = self.conn.execute(
                    "INSERT INTO um_facts(owner, category, name, body, importance,"
                    " created_at, updated_at, valid_until, superseded_by)"
                    " VALUES(?,?,?,?,?,?,?,0,0)",
                    (owner, category, name, body, importance, now, now))
            except sqlite3.IntegrityError:
                self.conn.rollback()
                if _depth >= 1:
                    raise
                return self._upsert_fact(category, name, body, importance, owner,
                                         _depth + 1)
            nid = cur.lastrowid
            self.conn.execute(
                "UPDATE um_facts SET superseded_by=? WHERE id=?", (nid, fid))
            # Рёбра старой версии истекают вместе с ней (граф не отдаёт stale).
            self.conn.execute(
                "UPDATE um_edges SET valid_until=? WHERE fact_id=? AND valid_until=0",
                (now, fid))
            self._fts_index("um_facts", nid, f"{name} {body}")
            self.conn.commit()
            return {"id": nid, "status": "superseded", "superseded_id": fid}
        try:
            cur = self.conn.execute(
                "INSERT INTO um_facts(owner, category, name, body, importance,"
                " created_at, updated_at, valid_until, superseded_by)"
                " VALUES(?,?,?,?,?,?,?,0,0)",
                (owner, category, name, body, importance, now, now))
        except sqlite3.IntegrityError:
            # backstop: слот занят вне этого процесса — перечитать и свести
            self.conn.rollback()
            if _depth >= 1:
                raise
            return self._upsert_fact(category, name, body, importance, owner,
                                     _depth + 1)
        fid = cur.lastrowid
        self._fts_index("um_facts", fid, f"{name} {body}")
        self.conn.commit()
        return {"id": fid, "status": "created", "superseded_id": 0}

    @_locked
    def update_fact(self, fid: int, body: str | None = None,
                    importance: float | None = None,
                    valid_until: float | None = None,
                    owner: str = "") -> dict | None:
        """Правка факта по id без потери provenance.

        valid_until in-place (0 = reopen), затем body/importance. Новое тело =
        новая версия (supersede), id меняется, chain фиксируется в superseded_by.
        """
        row = self.conn.execute(
            "SELECT owner, category, name, body, importance, valid_until"
            " FROM um_facts WHERE id=?", (fid,)).fetchone()
        if not row:
            return None
        r_owner, cat, name, cur_body, cur_imp, cur_vu = row
        if owner and (r_owner or "") != owner:
            return None
        if valid_until is not None:
            self.conn.execute(
                "UPDATE um_facts SET valid_until=?, updated_at=? WHERE id=?",
                (float(valid_until), time.time(), fid))
            self.conn.commit()
            status = "reopened" if float(valid_until) == 0 else "expired"
            return {"id": fid, "status": status, "superseded_id": 0}
        if body is not None and body != cur_body:
            if cur_vu > 0:
                raise ValueError(
                    "cannot edit expired fact; reopen with valid_until=0 first")
            return self._upsert_fact(
                cat, name, body,
                importance if importance is not None else cur_imp,
                r_owner or "")
        if importance is not None and abs(importance - cur_imp) > 1e-9:
            self.conn.execute(
                "UPDATE um_facts SET importance=?, updated_at=? WHERE id=?",
                (max(0.0, min(1.0, importance)), time.time(), fid))
            self.conn.commit()
            return {"id": fid, "status": "updated", "superseded_id": 0}
        return {"id": fid, "status": "noop", "superseded_id": 0}

    @_locked
    def update_edge(self, eid: int, valid_until: float, owner: str = "") -> bool:
        """Истечение/reopen ребра (mnemosyne triple_end). Замена = новое ребро."""
        row = self.conn.execute(
            "SELECT owner FROM um_edges WHERE id=?", (eid,)).fetchone()
        if not row:
            return False
        if owner and (row[0] or "") != owner:
            return False
        self.conn.execute("UPDATE um_edges SET valid_until=? WHERE id=?",
                          (float(valid_until), eid))
        self.conn.commit()
        return True

    def _dedupe_live_slots(self) -> None:
        """Lossless дедуп живых слотов (legacy): keeper = max(id), прочие superseded.

        Без лока — из __init__ до создания partial unique index: без этого
        CREATE UNIQUE INDEX упал бы на БД с дублями (IntegrityError).
        """
        rows = self.conn.execute(
            "SELECT owner, category, name, id FROM um_facts WHERE valid_until=0"
            " ORDER BY owner, category, name, id").fetchall()
        keeper: dict[tuple, int] = {}
        doomed: list[tuple[int, int]] = []
        for owner, cat, name, fid in rows:
            key = (owner, cat, name)
            prev = keeper.get(key)
            if prev is not None:
                doomed.append((prev, fid))  # prev старше -> superseded новым
            keeper[key] = fid
        if not doomed:
            return
        now = time.time()
        for old, new in doomed:
            self.conn.execute(
                "UPDATE um_facts SET valid_until=?, superseded_by=? WHERE id=?",
                (now, new, old))
        self.conn.commit()

    @_locked
    def validity_for(self, refs: list[tuple[str, int]]) -> dict[tuple[str, int], float]:
        """Batch valid_until для фактов/рёбер (0 = живое). Прочие таблицы без TTL."""
        out: dict[tuple[str, int], float] = {}
        by_table: dict[str, list[int]] = {}
        for ot, oid in refs:
            if ot in ("um_facts", "um_edges"):
                by_table.setdefault(ot, []).append(oid)
        for ot, oids in by_table.items():
            ph = ",".join("?" * len(oids))
            for oid, vu in self.conn.execute(
                    f"SELECT id, valid_until FROM {ot} WHERE id IN ({ph})", oids):
                out[(ot, oid)] = float(vu or 0.0)
        return out

    @_locked
    def window_for(self, refs: list[tuple[str, int]]
                   ) -> dict[tuple[str, int], tuple[float, float]]:
        """Batch окно валидности (valid_from=created_at, valid_until) фактов/рёбер."""
        out: dict[tuple[str, int], tuple[float, float]] = {}
        by_table: dict[str, list[int]] = {}
        for ot, oid in refs:
            if ot in ("um_facts", "um_edges"):
                by_table.setdefault(ot, []).append(oid)
        for ot, oids in by_table.items():
            ph = ",".join("?" * len(oids))
            for oid, ca, vu in self.conn.execute(
                    f"SELECT id, created_at, valid_until FROM {ot}"
                    f" WHERE id IN ({ph})", oids):
                out[(ot, oid)] = (float(ca or 0.0), float(vu or 0.0))
        return out

    @_locked
    def row_meta(self, owner_table: str, owner_id: int) -> dict:
        """Версионные поля для mem_expand (fact/edge)."""
        if owner_table == "um_facts":
            r = self.conn.execute(
                "SELECT valid_until, superseded_by FROM um_facts WHERE id=?",
                (owner_id,)).fetchone()
            return {"valid_until": float(r[0]), "superseded_by": int(r[1])} if r else {}
        if owner_table == "um_edges":
            r = self.conn.execute(
                "SELECT valid_until, created_at FROM um_edges WHERE id=?",
                (owner_id,)).fetchone()
            # valid_from = created_at: ребро валидно с момента вставки
            return {"valid_until": float(r[0]), "valid_from": float(r[1])} if r else {}
        return {}

    @_locked
    def add_summary(self, session_id: str, body: str, depth: int = 0,
                    covers_from: int | None = None, covers_to: int | None = None,
                    owner: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO um_summaries(session_id, owner, depth, body, covers_from, covers_to, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (session_id, owner, depth, body, covers_from, covers_to, time.time()),
        )
        sid = cur.lastrowid
        self._fts_index("um_summaries", sid, body)
        self.bump_tokens(session_id, estimate_tokens(body), owner)
        self.conn.commit()
        return sid

    @_locked
    def add_vector(self, owner_table: str, owner_id: int,
                   vec: list[float], model: str, owner: str = "") -> None:
        # Replace-семантика (#5): повторный embed того же owner не плодит дубли.
        self.conn.execute(
            "DELETE FROM um_vectors WHERE owner_table=? AND owner_id=?",
            (owner_table, owner_id),
        )
        self.conn.execute(
            "INSERT INTO um_vectors(owner_table, owner_id, embedding, model, owner)"
            " VALUES(?,?,?,?,?)",
            (owner_table, owner_id, pack_vector(vec), model, owner),
        )
        if self._vec_ready(len(vec)):
            try:
                self.conn.execute(
                    "DELETE FROM um_vecidx WHERE owner_table=? AND owner_id=?",
                    (owner_table, owner_id))
                self.conn.execute(
                    "INSERT INTO um_vecidx(embedding, owner_table, owner_id, owner)"
                    " VALUES(?,?,?,?)",
                    (_VEC_MOD["mod"].serialize_float32(vec),
                     owner_table, owner_id, owner))
            except Exception:
                # индекс битый (снесли таблицу вручную) — размечаем как потерянный,
                # источник правды um_vectors цел, пересборка через reindex
                self.conn.execute("DELETE FROM um_meta WHERE key='vec_index_dim'")
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
    def get_message(self, mid: int, owner: str = "") -> dict | None:
        row = self.conn.execute(
            "SELECT id, session_id, owner, role, content, created_at, source"
            " FROM um_messages WHERE id=?", (mid,)).fetchone()
        if not row:
            return None
        d = dict(zip(["id", "session_id", "owner", "role", "content",
                      "created_at", "source"], row))
        if owner and d["owner"] != owner:
            return None  # чужой тенант: как будто нет
        return d

    @_locked
    def session_messages(self, session_id: str, after_id: int = 0,
                         limit: int = 50, owner: str = "") -> list[dict]:
        q = ("SELECT id, session_id, role, content, created_at, source FROM um_messages"
             " WHERE session_id=? AND id>?")
        params: list = [session_id, after_id]
        if owner:
            q += " AND owner=?"
            params.append(owner)
        rows = self.conn.execute(q + " ORDER BY id LIMIT ?", (*params, limit)).fetchall()
        keys = ["id", "session_id", "role", "content", "created_at", "source"]
        return [dict(zip(keys, r)) for r in rows]

    @_locked
    def fts_search(self, query: str, scope: str = "all",
                   session_id: str = "", limit: int = 20,
                   owner: str = "",
                   include_expired: bool = False,
                   as_of: float | None = None) -> list[Hit]:
        """Полнотекст: FTS5 при наличии, иначе LIKE по токенам.

        Trigram-FTS не ищет термы короче 3 символов («да», «он») — для таких
        запросов сразу идём в LIKE, а не возвращаем пусто (#1).

        owner="" — legacy без фильтра; непустой — строгая изоляция тенанта.
        """
        terms = tokenize(query)
        if not terms:
            return []
        tables = {"all": ["um_messages", "um_summaries", "um_facts", "um_edges"],
                  "session": ["um_messages", "um_summaries", "um_edges"],
                  "facts": ["um_facts"]}
        if scope not in tables:
            from .recall import VALID_SCOPES

            raise ValueError(f"unknown scope {scope!r}: {VALID_SCOPES}")
        tables = tables[scope]
        if self.fts and max(len(t) for t in terms) >= 3:
            match = " OR ".join(f'"{t}"' for t in terms[:10] if len(t) >= 3)
            q = ("SELECT owner_table, owner_id FROM um_fts WHERE um_fts MATCH ? LIMIT ?")
            try:
                rows = self.conn.execute(q, (match, limit * 3)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            cand = [(ot, oid) for ot, oid in rows if ot in tables]
            owners = self.owners_for(cand) if owner else {}
            expired: set = set()
            if as_of is not None:
                win = self.window_for(cand)
                expired = {k for k, (vf, vu) in win.items()
                           if not (vf <= as_of and (vu == 0 or vu > as_of))}
            elif not include_expired:
                now = time.time()
                expired = {k for k, vu in self.validity_for(cand).items()
                           if vu != 0 and vu <= now}
            hits = []
            for ot, oid in cand:
                if owner and owners.get((ot, oid), "") != owner:
                    continue
                if (ot, oid) in expired:
                    continue
                body, sid = self._body_of(ot, oid)
                if body is None:
                    continue
                if scope == "session" and ot in ("um_messages", "um_edges", "um_summaries") \
                        and sid != session_id:
                    continue
                hits.append(Hit(ot, oid, body, 1.0, sid))
                if len(hits) >= limit:
                    break
            return hits
        # LIKE fallback
        hits = []
        for ot in tables:
            if ot == "um_edges":
                # у рёбер нет текстовой колонки — ищем по predicate/именам сущностей
                cond = " OR ".join(
                    ["e.predicate LIKE ?", "s.name LIKE ?", "o.name LIKE ?"]
                    * len(terms[:6]))
                params = [f"%{t}%" for t in terms[:6] for _ in range(3)]
                if owner:
                    cond = (f"(e.owner=? AND s.owner=? AND o.owner=?) AND ({cond})")
                    params = [owner, owner, owner] + params
                if scope == "session":
                    cond = f"(e.session_id=?) AND ({cond})"
                    params = [session_id] + params
                if as_of is not None:
                    cond = (f"(e.created_at<=? AND (e.valid_until=0 OR e.valid_until>?))"
                            f" AND ({cond})")
                    params = [as_of, as_of] + params
                elif not include_expired:
                    cond = f"(e.valid_until=0 OR e.valid_until>?) AND ({cond})"
                    params = [time.time()] + params
                rows = self.conn.execute(
                    """SELECT e.id FROM um_edges e
                       JOIN um_entities s ON s.id = e.subject_id
                       JOIN um_entities o ON o.id = e.object_id
                       WHERE """ + cond + " LIMIT ?", (*params, limit)).fetchall()
                for (oid,) in rows:
                    body, sid = self._body_of("um_edges", oid)
                    if body is not None:
                        hits.append(Hit(ot, oid, body, 1.0, sid))
                continue
            col = "content" if ot == "um_messages" else "body"
            cond = " OR ".join([f"{col} LIKE ?"] * len(terms[:6]))
            params: list = [f"%{t}%" for t in terms[:6]]
            if owner and ot in ("um_messages", "um_summaries", "um_facts"):
                cond = f"(owner=?) AND ({cond})"
                params = [owner] + params
            if ot == "um_messages" and scope == "session":
                cond = f"(session_id=?) AND ({cond})"
                params = [session_id] + params
            if ot == "um_facts" and as_of is not None:
                cond = (f"(created_at<=? AND (valid_until=0 OR valid_until>?))"
                        f" AND ({cond})")
                params = [as_of, as_of] + params
            elif ot == "um_facts" and not include_expired:
                cond = f"(valid_until=0 OR valid_until>?) AND ({cond})"
                params = [time.time()] + params
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
                "SELECT body, session_id FROM um_summaries WHERE id=?", (owner_id,)).fetchone()
            return ((r[0], r[1]) if r else (None, ""))
        if owner_table == "um_edges":
            r = self.conn.execute(
                """SELECT s.display, e.predicate, o.display, e.session_id FROM um_edges e
                   JOIN um_entities s ON s.id = e.subject_id
                   JOIN um_entities o ON o.id = e.object_id
                   WHERE e.id=?""", (owner_id,)).fetchone()
            return ((f"{r[0]} --{r[1]}--> {r[2]}", r[3]) if r else (None, ""))
        if owner_table == "um_facts":
            r = self.conn.execute(
                "SELECT name, body FROM um_facts WHERE id=?", (owner_id,)).fetchone()
            return ((f"{r[0]}: {r[1]}", "") if r else (None, ""))
        return None, ""

    @_locked
    def all_vectors(self, owner_tables: list[str] | None = None,
                    owner: str = "") -> list[tuple[str, int, list[float]]]:
        q = "SELECT owner_table, owner_id, embedding FROM um_vectors"
        conds: list = []
        params: list = []
        if owner_tables:
            conds.append(f"owner_table IN ({','.join('?' * len(owner_tables))})")
            params += owner_tables
        if owner:
            conds.append("owner=?")
            params.append(owner)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        return [(ot, oid, unpack_vector(b)) for ot, oid, b in self.conn.execute(q, params)]

    @_locked
    def delete_fact(self, fid: int, owner: str = "") -> bool:
        if owner:
            row = self.conn.execute(
                "SELECT owner FROM um_facts WHERE id=?", (fid,)).fetchone()
            if not row or (row[0] or "") != owner:
                return False
        cur = self.conn.execute("DELETE FROM um_facts WHERE id=?", (fid,))
        self._vec_delete("um_facts", fid)
        self.conn.execute("DELETE FROM um_vectors WHERE owner_table='um_facts' AND owner_id=?", (fid,))
        if self.fts:
            self.conn.execute(
                "DELETE FROM um_fts WHERE owner_table='um_facts' AND owner_id=?", (fid,))
        # Каскад #4: рёбра, порождённые этим фактом, + их вектора/fts,
        # затем сущности-сироты (без оставшихся рёбер).
        edge_ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM um_edges WHERE fact_id=?", (fid,))]
        for eid in edge_ids:
            self.conn.execute("DELETE FROM um_edges WHERE id=?", (eid,))
            self.conn.execute(
                "DELETE FROM um_vectors WHERE owner_table='um_edges' AND owner_id=?", (eid,))
            if self.fts:
                self.conn.execute(
                    "DELETE FROM um_fts WHERE owner_table='um_edges' AND owner_id=?", (eid,))
        self.conn.execute(
            """DELETE FROM um_entities WHERE id NOT IN
               (SELECT subject_id FROM um_edges UNION SELECT object_id FROM um_edges)""")
        self.conn.execute(
            """DELETE FROM um_vectors WHERE owner_table='um_entities' AND owner_id NOT IN
               (SELECT id FROM um_entities)""")
        self.conn.commit()
        return cur.rowcount > 0

    @_locked
    def bodies_for(self, refs: list[tuple[str, int]]) -> dict[tuple[str, int], tuple[str | None, str]]:
        """Batch-версия _body_of: 1 запрос на таблицу вместо N+1."""
        out: dict[tuple[str, int], tuple[str | None, str]] = {}
        by_table: dict[str, list[int]] = {}
        for ot, oid in refs:
            by_table.setdefault(ot, []).append(oid)
        for ot, oids in by_table.items():
            ph = ",".join("?" * len(oids))
            if ot == "um_messages":
                rows = self.conn.execute(
                    f"SELECT id, content, session_id FROM um_messages WHERE id IN ({ph})",
                    oids).fetchall()
                for oid, body, sid in rows:
                    out[(ot, oid)] = (body, sid)
            elif ot == "um_summaries":
                rows = self.conn.execute(
                    f"SELECT id, body, session_id FROM um_summaries WHERE id IN ({ph})",
                    oids).fetchall()
                for oid, body, sid in rows:
                    out[(ot, oid)] = (body, sid)
            elif ot == "um_facts":
                rows = self.conn.execute(
                    f"SELECT id, name, body FROM um_facts WHERE id IN ({ph})",
                    oids).fetchall()
                for oid, name, body in rows:
                    out[(ot, oid)] = (f"{name}: {body}", "")
            elif ot == "um_edges":
                rows = self.conn.execute(
                    f"""SELECT e.id, s.display, e.predicate, o.display, e.session_id
                        FROM um_edges e
                        JOIN um_entities s ON s.id = e.subject_id
                        JOIN um_entities o ON o.id = e.object_id
                        WHERE e.id IN ({ph})""", oids).fetchall()
                for oid, sname, pred, oname, sid in rows:
                    out[(ot, oid)] = (f"{sname} --{pred}--> {oname}", sid)
        return out

    @_locked
    def created_for(self, refs: list[tuple[str, int]]) -> dict[tuple[str, int], float]:
        """Batch timestamps для recency-приора: 1 запрос на таблицу, не N+1."""
        out: dict[tuple[str, int], float] = {}
        by_table: dict[str, list[int]] = {}
        for ot, oid in refs:
            by_table.setdefault(ot, []).append(oid)
        for ot, oids in by_table.items():
            if ot not in ("um_messages", "um_summaries", "um_facts", "um_edges"):
                continue
            ph = ",".join("?" * len(oids))
            for oid, ts in self.conn.execute(
                    f"SELECT id, created_at FROM {ot} WHERE id IN ({ph})", oids):
                out[(ot, oid)] = float(ts or 0.0)
        return out

    @_locked
    def owners_for(self, refs: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
        """Batch владельцев для owner-фильтра FTS/graph-arm (1 запрос на таблицу)."""
        out: dict[tuple[str, int], str] = {}
        by_table: dict[str, list[int]] = {}
        for ot, oid in refs:
            by_table.setdefault(ot, []).append(oid)
        for ot, oids in by_table.items():
            if ot not in ("um_messages", "um_summaries", "um_facts", "um_edges"):
                continue
            ph = ",".join("?" * len(oids))
            for oid, own in self.conn.execute(
                    f"SELECT id, owner FROM {ot} WHERE id IN ({ph})", oids):
                out[(ot, oid)] = own or ""
        return out

    @_locked
    def recent(self, start_ts: float, end_ts: float, session_id: str = "",
               owner: str = "", limit: int = 20) -> list[dict]:
        """Temporal выборка поверх messages+summaries: [start, end), свежие first."""
        out: list[dict] = []
        mq = ("SELECT id, session_id, content, created_at FROM um_messages"
              " WHERE created_at >= ? AND created_at < ?")
        mp: list = [start_ts, end_ts]
        if session_id:
            mq += " AND session_id=?"
            mp.append(session_id)
        if owner:
            mq += " AND owner=?"
            mp.append(owner)
        for mid, sid, body, ts in self.conn.execute(
                mq + " ORDER BY created_at DESC, id DESC LIMIT ?", (*mp, limit)):
            out.append({"kind": "um_messages", "id": mid, "session_id": sid,
                        "body": body, "created_at": ts})
        sq = ("SELECT id, session_id, body, created_at FROM um_summaries"
              " WHERE created_at >= ? AND created_at < ?")
        sp: list = [start_ts, end_ts]
        if session_id:
            sq += " AND session_id=?"
            sp.append(session_id)
        if owner:
            sq += " AND owner=?"
            sp.append(owner)
        for sid_, ssid, body, ts in self.conn.execute(
                sq + " ORDER BY created_at DESC, id DESC LIMIT ?", (*sp, limit)):
            out.append({"kind": "um_summaries", "id": sid_, "session_id": ssid,
                        "body": body, "created_at": ts})
        out.sort(key=lambda r: (-r["created_at"], -r["id"]))
        return out[:limit]

    @_locked
    def vectors_for(self, refs: list[tuple[str, int]]) -> dict[tuple[str, int], list[float]]:
        """Batch векторов по (table, id) — KNN-перескоринг косинусом, без фулскана."""
        out: dict[tuple[str, int], list[float]] = {}
        by_table: dict[str, list[int]] = {}
        for ot, oid in refs:
            by_table.setdefault(ot, []).append(oid)
        for ot, oids in by_table.items():
            if ot not in _VEC_TABLES:
                continue
            ph = ",".join("?" * len(oids))
            for oid, blob in self.conn.execute(
                    "SELECT owner_id, embedding FROM um_vectors"
                    f" WHERE owner_table=? AND owner_id IN ({ph})", (ot, *oids)):
                try:
                    out[(ot, oid)] = unpack_vector(blob)
                except Exception:
                    continue
        return out

    def _vec_ensure(self) -> bool:
        """Load sqlite-vec в коннект (кеш на инстанс). Без лока — из locked."""
        if getattr(self, "_vec_loaded", False):
            return True
        if not vec_extension_available():
            return False
        try:
            self.conn.enable_load_extension(True)
            _VEC_MOD["mod"].load(self.conn)
            self.conn.enable_load_extension(False)
        except Exception:
            return False
        self._vec_loaded = True
        return True

    def _vec_dim(self) -> int:
        """Dim активного индекса из um_meta (0 = нет). RLock — из locked."""
        raw = self.meta_get("vec_index_dim")
        return int(raw) if raw and raw.isdigit() else 0

    def _vec_ready(self, dim: int) -> bool:
        """Индекс существует и под этот dim. Без лока — из locked-контекста."""
        if not dim or self._vec_dim() != dim or not self._vec_ensure():
            return False
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='um_vecidx'").fetchone() is not None

    def _vec_delete(self, owner_table: str, owner_id: int) -> None:
        """Убрать строку из индекса. Без лока — из locked; тихий noop без индекса."""
        if not self._vec_dim():
            return
        try:
            self.conn.execute(
                "DELETE FROM um_vecidx WHERE owner_table=? AND owner_id=?",
                (owner_table, owner_id))
        except Exception:
            pass  # индекс снесли вручную — источник правды um_vectors, не он

    @_locked
    def build_vec_index(self, dim: int) -> int:
        """Построить/пересобрать vec0-индекс под dim. Источник правды — um_vectors."""
        if not self._vec_ensure():
            raise RuntimeError("sqlite-vec unavailable: pip install -e .[local-vec]")
        if dim <= 0:
            raise ValueError("build_vec_index needs dim > 0")
        self.conn.execute("DROP TABLE IF EXISTS um_vecidx")
        self.conn.execute(
            "CREATE VIRTUAL TABLE um_vecidx USING vec0("
            f"embedding float[{int(dim)}],"
            " owner_table TEXT, owner_id INTEGER, owner TEXT)")
        ser = _VEC_MOD["mod"].serialize_float32
        n = 0
        for ot, oid, blob, own in self.conn.execute(
                "SELECT owner_table, owner_id, embedding, owner FROM um_vectors"):
            try:
                vec = unpack_vector(blob)
            except Exception:
                continue
            if len(vec) != dim:
                continue
            self.conn.execute(
                "INSERT INTO um_vecidx(embedding, owner_table, owner_id, owner)"
                " VALUES(?,?,?,?)", (ser(vec), ot, oid, own or ""))
            n += 1
        self.meta_set("vec_index_dim", str(dim))
        self.conn.commit()
        return n

    @_locked
    def vec_index_status(self) -> dict:
        if not vec_extension_available():
            return {"mode": "unavailable"}
        dim = self._vec_dim()
        if not dim:
            return {"mode": "off"}
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='um_vecidx'").fetchone()
        if not exists:
            return {"mode": "missing", "dim": dim}
        return {"mode": "ready", "dim": dim}

    @_locked
    def knn(self, qvec: list[float], tables: list[str] | None,
            owner: str = "", k: int = 10) -> list[tuple[str, int, float]] | None:
        """KNN-кандидаты (таблица, id, L2). None = индекс неприменим → brute force.

        По одному запросу на таблицу: vec0 не любит IN в KNN-фильтрах,
        а пост-фильтр голодал бы топ-k чужими таблицами.
        """
        if not self._vec_ready(len(qvec)):
            return None
        wanted = [t for t in (tables or list(_VEC_TABLES)) if t in _VEC_TABLES]
        qblob = _VEC_MOD["mod"].serialize_float32(list(map(float, qvec)))
        out: list[tuple[str, int, float]] = []
        for ot in wanted:
            q = ("SELECT owner_id, distance FROM um_vecidx"
                 " WHERE embedding MATCH ?"
                 f" AND k = {int(k)} AND owner_table = ?")
            params: list = [qblob, ot]
            if owner:
                q += " AND owner = ?"
                params.append(owner)
            try:
                rows = self.conn.execute(q, params).fetchall()
            except Exception:
                return None  # странный KNN — честный фолбэк, не взрыв
            out += [(ot, oid, dist) for oid, dist in rows]
        return out

    @_locked
    def has_vector(self, owner_table: str, owner_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM um_vectors WHERE owner_table=? AND owner_id=?",
            (owner_table, owner_id)).fetchone() is not None

    def _drop_edge_rows(self, eid: int) -> None:
        """Без лока — вызывать из locked-контекста."""
        self._vec_delete("um_edges", eid)
        self.conn.execute("DELETE FROM um_edges WHERE id=?", (eid,))
        self.conn.execute(
            "DELETE FROM um_vectors WHERE owner_table='um_edges' AND owner_id=?", (eid,))
        if self.fts:
            self.conn.execute(
                "DELETE FROM um_fts WHERE owner_table='um_edges' AND owner_id=?", (eid,))

    def _prune_orphan_entities(self) -> None:
        self.conn.execute(
            """DELETE FROM um_entities WHERE id NOT IN
               (SELECT subject_id FROM um_edges UNION SELECT object_id FROM um_edges)""")
        self.conn.execute(
            """DELETE FROM um_vectors WHERE owner_table='um_entities' AND owner_id NOT IN
               (SELECT id FROM um_entities)""")
        if self._vec_dim():
            try:
                self.conn.execute(
                    """DELETE FROM um_vecidx WHERE owner_table='um_entities'
                       AND owner_id NOT IN (SELECT id FROM um_entities)""")
            except Exception:
                pass  # индекса нет — нечего чистить

    @_locked
    def delete_edge(self, eid: int, owner: str = "") -> bool:
        """Прямое удаление ребра — закрывает дыру бессмертных fact_id=0 (#3)."""
        row = self.conn.execute(
            "SELECT owner FROM um_edges WHERE id=?", (eid,)).fetchone()
        if not row:
            return False
        if owner and (row[0] or "") != owner:
            return False
        self._drop_edge_rows(eid)
        self._prune_orphan_entities()
        self.conn.commit()
        return True

    @_locked
    def delete_entity(self, name: str, owner: str = "") -> bool:
        """Удалить сущность + все её рёбра каскадом."""
        key = name.strip().lower()
        q = "SELECT id FROM um_entities WHERE name=?"
        params: list = [key]
        if owner:
            q += " AND owner=?"
            params.append(owner)
        row = self.conn.execute(q, params).fetchone()
        if not row:
            return False
        ent_id = row[0]
        for (eid,) in self.conn.execute(
                "SELECT id FROM um_edges WHERE subject_id=? OR object_id=?",
                (ent_id, ent_id)):
            self._drop_edge_rows(eid)
        self._vec_delete("um_entities", ent_id)
        self.conn.execute("DELETE FROM um_entities WHERE id=?", (ent_id,))
        self.conn.execute(
            "DELETE FROM um_vectors WHERE owner_table='um_entities' AND owner_id=?",
            (ent_id,))
        self._prune_orphan_entities()
        self.conn.commit()
        return True

    # -- graph ----------------------------------------------------------
    @_locked
    def add_entity(self, name: str, owner: str = "") -> int:
        import time as _t
        key = name.strip().lower()
        row = self.conn.execute(
            "SELECT id FROM um_entities WHERE name=? AND owner=?", (key, owner)).fetchone()
        if row:
            return row[0]
        cur = self.conn.execute(
            "INSERT INTO um_entities(name, display, created_at, owner) VALUES(?,?,?,?)",
            (key, name.strip(), _t.time(), owner))
        self.conn.commit()
        return cur.lastrowid

    @_locked
    def add_edge(self, subject: str, predicate: str, obj: str,
                 session_id: str = "", fact_id: int = 0, owner: str = "") -> int:
        import time as _t
        sid = self.add_entity(subject, owner)
        oid = self.add_entity(obj, owner)
        cur = self.conn.execute(
            "INSERT INTO um_edges(subject_id, predicate, object_id, session_id,"
            " fact_id, created_at, owner) VALUES(?,?,?,?,?,?,?)",
            (sid, predicate.strip().lower(), oid, session_id, fact_id, _t.time(), owner))
        eid = cur.lastrowid
        self._fts_index("um_edges", eid, f"{subject} {predicate} {obj}")
        self.conn.commit()
        return eid

    @_locked
    def neighbors(self, entity_name: str, session_id: str = "",
                  limit: int = 100, owner: str = "",
                  include_expired: bool = False,
                  as_of: float | None = None) -> list[dict]:
        """as_of: срез графа на момент — valid_from(=created_at) <= as_of < valid_until."""
        key = entity_name.strip().lower()
        q = "SELECT id FROM um_entities WHERE name=?"
        params: list = [key]
        if owner:
            q += " AND owner=?"
            params.append(owner)
        row = self.conn.execute(q, params).fetchone()
        if not row:
            return []
        eid = row[0]
        q = """SELECT e.id, s.display, e.predicate, o.display, e.session_id
               FROM um_edges e
               JOIN um_entities s ON s.id = e.subject_id
               JOIN um_entities o ON o.id = e.object_id
               WHERE (e.subject_id = ? OR e.object_id = ?)"""
        params = [eid, eid]
        if session_id:
            q += " AND e.session_id = ?"
            params.append(session_id)
        if owner:
            q += " AND e.owner = ?"
            params.append(owner)
        if as_of is not None:
            # ось валидности: ребро жило в [created_at, valid_until)
            q += " AND e.created_at <= ? AND (e.valid_until = 0 OR e.valid_until > ?)"
            params += [as_of, as_of]
        elif not include_expired:
            q += " AND (e.valid_until = 0 OR e.valid_until > ?)"
            params.append(time.time())
        q += " LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(q, params).fetchall()
        return [dict(zip(["edge_id", "subject", "predicate", "object", "session_id"], r))
                for r in rows]

    @_locked
    def match_entities(self, terms: list[str], limit: int = 10,
                       owner: str = "") -> list[str]:
        out = []
        for t in terms[:8]:
            esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            q = "SELECT name FROM um_entities WHERE name LIKE ? ESCAPE '\\'"
            params: list = [f"%{esc}%"]
            if owner:
                q += " AND owner=?"
                params.append(owner)
            for r in self.conn.execute(q + " LIMIT ?", (*params, limit)):
                if r[0] not in out:
                    out.append(r[0])
        return out[:limit]

    @_locked
    def stats(self) -> dict:
        out = {}
        for t in ["um_messages", "um_summaries", "um_facts", "um_vectors",
                  "um_entities", "um_edges"]:
            out[t] = self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        out["fts"] = self.fts
        out["embedding_model"] = self.meta_get("embedding_model")
        try:
            out["owners"] = sorted(
                r[0] or "" for r in self.conn.execute(
                    "SELECT owner FROM um_messages UNION SELECT owner FROM um_facts"
                    " UNION SELECT owner FROM um_edges").fetchall())
        except Exception:
            out["owners"] = []
        return out

    @_locked
    def diagnostics(self) -> dict:
        """#6: integrity + вектора по моделям через лок."""
        integ = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        vec_rows = self.conn.execute(
            "SELECT model, count(*) FROM um_vectors GROUP BY model").fetchall()
        return {"integrity": integ,
                "vectors_by_model": [list(r) for r in vec_rows],
                "vec_index": self.vec_index_status(),
                "fts": self.fts}

    @_locked
    def hygiene(self) -> dict:
        """Кандидаты мусора без мутаций: сироты векторов/FTS/сущностей, висячий vec0."""
        out: dict = {"orphan_vectors": [], "orphan_fts": [],
                     "orphan_entities": 0, "dangling_vecidx": []}
        parents = {"um_messages": "SELECT id FROM um_messages",
                   "um_summaries": "SELECT id FROM um_summaries",
                   "um_facts": "SELECT id FROM um_facts",
                   "um_edges": "SELECT id FROM um_edges",
                   "um_entities": "SELECT id FROM um_entities"}
        for ot, psql in parents.items():
            for (oid,) in self.conn.execute(
                    "SELECT owner_id FROM um_vectors WHERE owner_table=?"
                    f" AND owner_id NOT IN ({psql}) LIMIT 11", (ot,)):
                out["orphan_vectors"].append([ot, oid])
            if self.fts:
                try:
                    rows = self.conn.execute(
                        "SELECT owner_id FROM um_fts WHERE owner_table=?"
                        f" AND owner_id NOT IN ({psql}) LIMIT 11", (ot,)).fetchall()
                except Exception:
                    rows = []
                out["orphan_fts"] += [[ot, r[0]] for r in rows]
        out["orphan_entities"] = self.conn.execute(
            """SELECT count(*) FROM um_entities WHERE id NOT IN
               (SELECT subject_id FROM um_edges UNION SELECT object_id FROM um_edges)"""
        ).fetchone()[0]
        if self._vec_dim():
            try:
                for ot, oid in self.conn.execute(
                        """SELECT v.owner_table, v.owner_id FROM um_vecidx v
                           LEFT JOIN um_vectors u ON u.owner_table=v.owner_table
                           AND u.owner_id=v.owner_id
                           WHERE u.id IS NULL LIMIT 11"""):
                    out["dangling_vecidx"].append([ot, oid])
            except Exception:
                pass
        return out

    @_locked
    def repair(self, dim: int = 0, backup_path: str = "") -> dict:
        """Backup-first ремонт: бэкап VACUUM INTO, чистка сирот, пересборка FTS/vec.

        dim — размерность для vec-индекса (0 = пропустить vec, только FTS+сироты).
        """
        import time as _t

        if not backup_path:
            backup_path = f"{self._db_path}.backup-{int(_t.time())}"
        safe_path = backup_path.replace("'", "''")
        self.conn.execute(f"VACUUM INTO '{safe_path}'")
        report: dict = {"backup": backup_path}
        dirty = self.hygiene()
        for ot, oid in dirty["orphan_vectors"]:
            self.conn.execute(
                "DELETE FROM um_vectors WHERE owner_table=? AND owner_id=?",
                (ot, oid))
        report["purged_vectors"] = len(dirty["orphan_vectors"])
        if self.fts:
            for ot, oid in dirty["orphan_fts"]:
                self.conn.execute(
                    "DELETE FROM um_fts WHERE owner_table=? AND owner_id=?",
                    (ot, oid))
            report["purged_fts"] = len(dirty["orphan_fts"])
            # Полная пересборка FTS из родителей (формат тел — как _fts_index).
            self.conn.execute("DELETE FROM um_fts")
            self.conn.execute(
                "INSERT INTO um_fts(owner_table, owner_id, body)"
                " SELECT 'um_messages', id, content FROM um_messages")
            self.conn.execute(
                "INSERT INTO um_fts(owner_table, owner_id, body)"
                " SELECT 'um_summaries', id, body FROM um_summaries")
            self.conn.execute(
                "INSERT INTO um_fts(owner_table, owner_id, body)"
                " SELECT 'um_facts', id, name || ' ' || body FROM um_facts")
            self.conn.execute(
                """INSERT INTO um_fts(owner_table, owner_id, body)
                   SELECT 'um_edges', e.id, s.display || ' ' || e.predicate || ' ' || o.display
                   FROM um_edges e JOIN um_entities s ON s.id=e.subject_id
                   JOIN um_entities o ON o.id=e.object_id""")
            report["fts_rebuilt"] = True
        self._prune_orphan_entities()
        if dim > 0:
            try:
                report["vec_index"] = self.build_vec_index(dim)
            except Exception as e:
                report["vec_index_error"] = f"{type(e).__name__}: {e}"[:200]
        self.conn.commit()
        return report

    @_locked
    def close(self) -> None:
        self.conn.close()


def open_db(cfg: Config, embedding_dim: int, embedding_model: str):
    """Совместимость со stage-0 API: возвращает Store."""
    return Store(cfg, embedding_dim, embedding_model)
