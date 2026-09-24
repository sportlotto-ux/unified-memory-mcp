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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .embeddings import check_store_dim
from .file_permissions import ensure_private_parent, restrict_new_sqlite_files


def _links_table_sql(name: str, *, if_not_exists: bool = True) -> str:
    exists = "IF NOT EXISTS " if if_not_exists else ""
    return f"""CREATE TABLE {exists}{name} (
    id INTEGER PRIMARY KEY,
    src_table TEXT NOT NULL CHECK (src_table IN
        ('um_messages', 'um_facts', 'um_summaries', 'um_edges')),
    src_id INTEGER NOT NULL,
    dst_table TEXT NOT NULL CHECK (dst_table IN
        ('um_messages', 'um_facts', 'um_summaries', 'um_edges')),
    dst_id INTEGER NOT NULL,
    rel TEXT NOT NULL CHECK (rel IN
        ('supports', 'contradicts', 'supersedes', 'derives_from')),
    weight REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0),
    owner TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    valid_until REAL NOT NULL DEFAULT 0  -- 0 = живое
);"""


SCHEMA = f"""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS um_messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',  -- v0.4-п.2: тенант; '' = legacy без изоляции
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'unknown',
    externalized_ref TEXT,
    conversation_id TEXT NOT NULL DEFAULT '',
    source_order INTEGER NOT NULL DEFAULT 0,
    source_ref TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT ''
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
    created_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT ''
);


CREATE TABLE IF NOT EXISTS um_summary_sources (
    summary_id INTEGER NOT NULL,
    source_table TEXT NOT NULL CHECK (source_table IN
        ('um_messages', 'um_summaries')),
    source_id INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (summary_id, source_table, source_id)
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
    metadata_json TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    veracity TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
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
    valid_until REAL NOT NULL DEFAULT 0,  -- 0 = живое; замена ребра = новое ребро
    metadata_json TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    veracity TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT ''
);

-- ADR-001: типизированные связи памяти (message<->fact, fact<->fact).
-- Traversal-only: ни FTS, ни векторов; um_edges (entity-граф) не трогаем.
{_links_table_sql("um_links")}

-- P2.2: пометки поверх refs (metadata-only: ни FTS, ни векторов, recall не меняют).
CREATE TABLE IF NOT EXISTS um_annotations (
    id INTEGER PRIMARY KEY,
    target_table TEXT NOT NULL CHECK (target_table IN
        ('um_messages', 'um_facts', 'um_summaries', 'um_edges')),
    target_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN
        ('useful', 'disputed', 'correction', 'note')),
    value TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    owner TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_um_messages_session ON um_messages(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_um_messages_owner ON um_messages(owner, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_summaries_session ON um_summaries(session_id, depth)",
    "CREATE INDEX IF NOT EXISTS idx_um_summaries_owner ON um_summaries(owner, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_summary_sources_summary"
    " ON um_summary_sources(summary_id, position)",
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
    # ADR-001: связи. D3 — одна живая связь на (src, dst, rel, owner).
    "CREATE INDEX IF NOT EXISTS idx_um_links_src ON um_links(src_table, src_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_links_dst ON um_links(dst_table, dst_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_links_owner ON um_links(owner)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_um_links_live ON um_links("
    "src_table, src_id, dst_table, dst_id, rel, owner) WHERE valid_until = 0",
    # P2.2: пометки. Дedupe — одна пометка на (target, kind, value, owner).
    "CREATE INDEX IF NOT EXISTS idx_um_annotations_target"
    " ON um_annotations(target_table, target_id)",
    "CREATE INDEX IF NOT EXISTS idx_um_annotations_owner ON um_annotations(owner)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_um_annotations_dedupe ON um_annotations("
    "target_table, target_id, kind, value, owner)",
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
    """Токены текста: tiktoken (если установлен), иначе RU-aware эвристика.

    `len//4` занижает кириллицу ~2.3x (cl100k ≈ 1.8 симв/токен), из-за чего
    компакшн на чистой инсталляции молча срабатывал позже. Считаем
    ASCII/4 + не-ASCII/2. Точный cl100k — `pip install '.[tokens]'`.
    """
    enc = _tiktoken_enc()
    if enc is not None:
        return len(enc.encode(text))
    ascii_n = sum(1 for ch in text if ch < "\x80")
    other = len(text) - ascii_n
    return max(1, (ascii_n + 3) // 4 + (other + 1) // 2)


_KIND_RANK = {"um_messages": 0, "um_summaries": 1}


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
    snippet: str = ""  # A4: FTS-сниппет (только FTS-плечо), тело остаётся полным
    archived: bool = False


# ADR-001: закрытые словари связей. Расширение — миграцией (CHECK + DDL).
LINK_RELS = ("supports", "contradicts", "supersedes", "derives_from")
LINK_TABLES = ("um_messages", "um_facts", "um_summaries", "um_edges")

# P2.2: пометки поверх тех же 4 таблиц. Расширение kind — миграцией (CHECK + DDL).
ANNOTATION_KINDS = ("useful", "disputed", "correction", "note")
ANNOTATION_TABLES = LINK_TABLES


_LINK_CHECK_PATTERNS = (
    re.compile(r"\bCHECK\s*\(\s*src_table\b", re.IGNORECASE),
    re.compile(r"\bCHECK\s*\(\s*dst_table\b", re.IGNORECASE),
    re.compile(r"\bCHECK\s*\(\s*rel\b", re.IGNORECASE),
    re.compile(r"\bCHECK\s*\(\s*weight\b", re.IGNORECASE),
)


class Store:
    """Синхронное ядро. Один инстанс на процесс (см. single-writer в MIGRATION_PLAN)."""

    def __init__(self, cfg: Config, embedding_dim: int = 0, embedding_model: str = "") -> None:
        self._db_path = str(cfg.db_path)
        self._artifact_seen: set[Path] = {
            Path(f"{self._db_path}{suffix}")
            for suffix in ("", "-wal", "-shm")
            if Path(f"{self._db_path}{suffix}").exists()
        }
        ensure_private_parent(cfg.db_path)
        self._lock = threading.RLock()
        self._sp_stack: list[str] = []  # стек активных savepoint'ов (batch/dry-run)
        self.conn = sqlite3.connect(str(cfg.db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self._recover_entity_migration()
        self._recover_link_migration()
        self.conn.executescript(SCHEMA)
        # Миграция существующих БД: ранние additive-колонки и display backfill.
        self._migrate_early_columns()
        # P1.6: migration adapters need source ordering and lossless metadata.
        self._migrate_source_metadata()
        # v0.4-п.2: owner-колонки. '' = legacy без изоляции, поведение не меняется.
        self._migrate_owner_columns()
        self._migrate_entity_owner()
        # v0.5: valid_until (sentinel 0 = живое) + superseded_by на фактах.
        self._migrate_validity_columns()
        # v0.7: um_links получил CHECK на концы/вес ПОСЛЕ первых прогонов.
        self._migrate_links_constraints()
        # P4.5: индекс создаётся строго ПОСЛЕ dedupe в том же проходе, значит его
        # наличие ⟹ dedupe уже отработал. Полный скан на каждом открытии не гоняем.
        _idx = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index'"
            " AND name='ux_um_facts_live'").fetchone()
        if not _idx:
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
        self.conn.execute(
            "INSERT OR IGNORE INTO um_meta(key, value) VALUES('schema_version','1')")
        self.conn.commit()
        self._restrict_artifacts()

    def _migrate_early_columns(self) -> None:
        """Apply early legacy columns and display backfill atomically."""
        pending: list[tuple[str, str, str]] = []
        edge_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(um_edges)")
        }
        if "fact_id" not in edge_cols:
            pending.append(
                ("um_edges", "fact_id", "INTEGER NOT NULL DEFAULT 0"))
        summary_cols = {
            row[1] for row in self.conn.execute(
                "PRAGMA table_info(um_summaries)")
        }
        if "superseded_by" not in summary_cols:
            pending.append(
                ("um_summaries", "superseded_by", "INTEGER NOT NULL DEFAULT 0"))
        entity_cols = {
            row[1] for row in self.conn.execute(
                "PRAGMA table_info(um_entities)")
        }
        display_missing = "display" not in entity_cols
        if display_missing:
            pending.append(
                ("um_entities", "display", "TEXT NOT NULL DEFAULT ''"))
        if not pending:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for table, col, ddl in pending:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            if display_missing:
                self.conn.execute(
                    "UPDATE um_entities SET display=name WHERE display=''")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _recover_entity_migration(self) -> None:
        """Recover the staging table left by the legacy owner migration."""
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "um_entities_new" not in names:
            return
        new_cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(um_entities_new)")}
        if "owner" not in new_cols:
            raise ValueError("invalid um_entities_new staging table")
        if "um_entities" not in names:
            self.conn.execute("ALTER TABLE um_entities_new RENAME TO um_entities")
            self.conn.commit()
            return
        old_cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(um_entities)")}
        old_count = self.conn.execute(
            "SELECT count(*) FROM um_entities").fetchone()[0]
        new_count = self.conn.execute(
            "SELECT count(*) FROM um_entities_new").fetchone()[0]
        if "owner" in old_cols and old_count == 0 and new_count:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("DROP TABLE um_entities")
                self.conn.execute(
                    "ALTER TABLE um_entities_new RENAME TO um_entities")
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            return
        # The old table is authoritative, or the staging copy is stale.
        self.conn.execute("DROP TABLE um_entities_new")
        self.conn.commit()

    def _migrate_entity_owner(self) -> None:
        """Rebuild legacy um_entities atomically for owner-aware uniqueness."""
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(um_entities)")}
        if "owner" in cols:
            return
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("""
                CREATE TABLE um_entities_new(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    display TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                    owner TEXT NOT NULL DEFAULT '', UNIQUE(name, owner));
            """)
            self.conn.execute(
                "INSERT INTO um_entities_new(id, name, display, created_at, owner) "
                "SELECT id, name, display, created_at, '' FROM um_entities")
            self.conn.execute("DROP TABLE um_entities")
            self.conn.execute(
                "ALTER TABLE um_entities_new RENAME TO um_entities")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _migrate_source_metadata(self) -> None:
        """Add lossless source/provenance fields for migration adapters."""
        specs = {
            "um_messages": (
                ("conversation_id", "TEXT NOT NULL DEFAULT ''"),
                ("source_order", "INTEGER NOT NULL DEFAULT 0"),
                ("source_ref", "TEXT NOT NULL DEFAULT ''"),
                ("metadata_json", "TEXT NOT NULL DEFAULT ''"),
            ),
            "um_summaries": (("metadata_json", "TEXT NOT NULL DEFAULT ''"),),
            "um_facts": (
                ("metadata_json", "TEXT NOT NULL DEFAULT ''"),
                ("confidence", "REAL NOT NULL DEFAULT 1.0"),
                ("veracity", "TEXT NOT NULL DEFAULT ''"),
                ("source_ref", "TEXT NOT NULL DEFAULT ''"),
            ),
            "um_edges": (
                ("metadata_json", "TEXT NOT NULL DEFAULT ''"),
                ("confidence", "REAL NOT NULL DEFAULT 1.0"),
                ("veracity", "TEXT NOT NULL DEFAULT ''"),
                ("source_ref", "TEXT NOT NULL DEFAULT ''"),
            ),
        }
        pending: list[tuple[str, str, str]] = []
        for table, columns in specs.items():
            present = {row[1] for row in self.conn.execute(
                f"PRAGMA table_info({table})")}
            pending.extend((table, name, ddl) for name, ddl in columns
                          if name not in present)
        if not pending:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for table, name, ddl in pending:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _migrate_owner_columns(self) -> None:
        """Add legacy owner columns as one resumable schema transaction."""
        tables = ("um_messages", "um_summaries", "um_facts",
                  "um_edges", "um_vectors")
        pending = [
            table for table in tables
            if "owner" not in {
                row[1] for row in self.conn.execute(
                    f"PRAGMA table_info({table})")
            }
        ]
        if not pending:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for table in pending:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN owner "
                    "TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _migrate_validity_columns(self) -> None:
        """Add legacy validity columns as one resumable schema transaction."""
        pending: list[tuple[str, str, str]] = []
        fact_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(um_facts)")
        }
        for col, ddl in (("valid_until", "REAL NOT NULL DEFAULT 0"),
                         ("superseded_by", "INTEGER NOT NULL DEFAULT 0")):
            if col not in fact_cols:
                pending.append(("um_facts", col, ddl))
        edge_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(um_edges)")
        }
        if "valid_until" not in edge_cols:
            pending.append(("um_edges", "valid_until", "REAL NOT NULL DEFAULT 0"))
        if not pending:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for table, col, ddl in pending:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    @staticmethod
    def _links_have_constraints(sql: str | None) -> bool:
        text = sql or ""
        return all(pattern.search(text) for pattern in _LINK_CHECK_PATTERNS)

    def _recover_link_migration(self) -> None:
        """Recover a staging table left by an interrupted links rebuild."""
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "um_links_new" not in names:
            return
        lrow = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table'"
            " AND name='um_links_new'"
        ).fetchone()
        new_cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(um_links_new)")}
        required = {
            "id", "src_table", "src_id", "dst_table", "dst_id", "rel",
            "weight", "owner", "session_id", "created_at", "valid_until",
        }
        if not self._links_have_constraints(lrow[0] if lrow else None):
            raise ValueError("invalid um_links_new staging table")
        if not required.issubset(new_cols):
            raise ValueError("invalid um_links_new staging columns")
        if "um_links" in names:
            # The old table remains authoritative; a staging table cannot be
            # trusted unless the old table is already absent.
            self.conn.execute("DROP TABLE um_links_new")
            self.conn.commit()
            return
        self.conn.execute("ALTER TABLE um_links_new RENAME TO um_links")
        self.conn.commit()

    def _migrate_links_constraints(self) -> None:
        """Rebuild legacy um_links atomically with endpoint and weight checks."""
        lrow = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table'"
            " AND name='um_links'"
        ).fetchone()
        if not lrow or self._links_have_constraints(lrow[0]):
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            old_count = self.conn.execute(
                "SELECT count(*) FROM um_links").fetchone()[0]
            self.conn.execute(_links_table_sql("um_links_new", if_not_exists=False))
            self.conn.execute("""
                INSERT INTO um_links_new(
                    id, src_table, src_id, dst_table, dst_id, rel, weight,
                    owner, session_id, created_at, valid_until)
                SELECT id, src_table, src_id, dst_table, dst_id, rel, weight,
                    owner, session_id, created_at, valid_until
                FROM um_links
            """)
            new_count = self.conn.execute(
                "SELECT count(*) FROM um_links_new").fetchone()[0]
            if new_count != old_count:
                raise RuntimeError("um_links migration row count mismatch")
            duplicate = self.conn.execute("""
                SELECT src_table, src_id, dst_table, dst_id, rel, owner
                FROM um_links
                WHERE valid_until = 0
                GROUP BY src_table, src_id, dst_table, dst_id, rel, owner
                HAVING count(*) > 1
                LIMIT 1
            """).fetchone()
            if duplicate is not None:
                raise ValueError("legacy um_links contains duplicate live links")
            self.conn.execute("DROP TABLE um_links")
            self.conn.execute(
                "ALTER TABLE um_links_new RENAME TO um_links")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

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
    def execute_write(self, sql: str, params: tuple = (),
                      _commit: bool = True) -> int:
        """#6: запись из движка только через лок. Возвращает rowcount."""
        cur = self.conn.execute(sql, params)
        self._commit_if(_commit)
        return cur.rowcount

    # -- транзакционное ядро (v0.7-п.5) -----------------------------------
    def _restrict_artifacts(self) -> None:
        restrict_new_sqlite_files(self._db_path, self._artifact_seen)

    def _commit_if(self, commit: bool) -> None:
        """Коммит, если вызов не находится внутри batch-транзакции (_commit=False)."""
        if commit:
            self.conn.commit()
            self._restrict_artifacts()

    def _abort(self) -> None:
        """Откат текущей операции: до активного savepoint'а (batch), иначе rollback."""
        if self._sp_stack:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {self._sp_stack[-1]}")
        else:
            self.conn.rollback()

    @contextmanager
    def savepoint(self, name: str = "um_op"):
        """Вложенный savepoint вокруг одной операции: ошибка откатывает ТОЛЬКО её."""
        if self._sp_stack:
            name = f"{name}_{len(self._sp_stack)}"
        self.conn.execute(f"SAVEPOINT {name}")
        self._sp_stack.append(name)
        try:
            yield
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.conn.execute(f"RELEASE SAVEPOINT {name}")
            self._sp_stack.pop()
            raise
        self.conn.execute(f"RELEASE SAVEPOINT {name}")
        self._sp_stack.pop()

    @contextmanager
    def read_locked(self):
        """Публичный RLock для внешних построчных сканеров (export, secret_scan).

        FastMCP-треды делят один conn: проход без лока — гонка итератора с
        писателем. RLock реентерабелен — безопасно вызывать из-под @_locked.
        Долгие сканы сериализуют писателей: цена консистентности прохода.
        """
        with self._lock:
            yield self.conn

    @contextmanager
    def transaction(self, dry_run: bool = False):
        """Одношовный batch-контур: RELEASE коммитит, dry_run/ошибка — ROLLBACK.
        Внутренние write-методы вызываются с _commit=False (иначе commit внутри
        разрывает контур: это и есть критический guardrail п.5).

        Push в _sp_stack обязателен: без него _abort() внутри батча ушёл бы в
        голый conn.rollback(), откатил весь батч, а последующий RELEASE закоммитил
        бы частичный прогресс как успех (P1a)."""
        name = "um_batch"
        with self._lock:  # P2: весь контур под одним RLock (single-writer)
            if name in self._sp_stack:
                raise ValueError("nested transaction() is not supported")
            self.conn.execute(f"SAVEPOINT {name}")
            self._sp_stack.append(name)
            ok = False
            try:
                yield
                ok = True
            finally:
                if dry_run or not ok:
                    self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                self.conn.execute(f"RELEASE SAVEPOINT {name}")
                self._sp_stack.pop()

    @_locked
    def _bump_metric(self, name: str, session_id: str, delta: int,
                     owner: str = "", _commit: bool = True) -> None:
        key = f"{name}:{owner}:{session_id}" if owner else f"{name}:{session_id}"
        self.conn.execute(
            "INSERT INTO um_meta(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE"
            " SET value=CAST(value AS INTEGER)+CAST(excluded.value AS INTEGER)",
            (key, str(delta)))
        self._commit_if(_commit)

    @_locked
    def bump_tokens(self, session_id: str, delta: int, owner: str = "",
                     _commit: bool = True) -> None:
        """Increment the legacy total pressure counter."""
        self._bump_metric("tokens", session_id, delta, owner, _commit)


    @_locked
    def bump_raw_tokens(self, session_id: str, delta: int, owner: str = "",
                        _commit: bool = True) -> None:
        """Increment uncovered raw-message pressure."""
        self._bump_metric("raw_tokens", session_id, delta, owner, _commit)

    @_locked
    def bump_summary_tokens(self, session_id: str, delta: int, owner: str = "",
                            _commit: bool = True) -> None:
        """Increment live-summary pressure."""
        self._bump_metric("summary_tokens", session_id, delta, owner, _commit)

    @_locked
    def meta_set(self, key: str, value: str, _commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO um_meta(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self._commit_if(_commit)

    # -- writes -----------------------------------------------------------
    @_locked
    def add_message(self, session_id: str, role: str, content: str,
                    source: str = "unknown", owner: str = "",
                    _commit: bool = True, conversation_id: str = "",
                    source_order: int = 0, source_ref: str = "",
                    metadata_json: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO um_messages(session_id, owner, role, content, created_at, source,"
            " externalized_ref, conversation_id, source_order, source_ref, metadata_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, owner, role, content, time.time(), source, None,
             conversation_id, int(source_order), source_ref, metadata_json),
        )
        mid = cur.lastrowid
        self._fts_index("um_messages", mid, content)
        tokens = estimate_tokens(content)
        self.bump_tokens(session_id, tokens, owner, _commit=False)
        self.bump_raw_tokens(session_id, tokens, owner, _commit=False)
        self._commit_if(_commit)
        return mid

    @_locked
    def add_fact(self, category: str, name: str, body: str,
                 importance: float = 0.5, owner: str = "",
                 _commit: bool = True) -> int:
        """Слот-запись: то же тело — no-op, новое — supersede-цепочка. Возвращает id живого."""
        return self._upsert_fact(category, name, body, importance, owner,
                                 _commit=_commit)["id"]

    @_locked
    def add_fact_ex(self, category: str, name: str, body: str,
                    importance: float = 0.5, owner: str = "",
                    _commit: bool = True) -> dict:
        """Слот-запись с полным статусом: {id, status, superseded_id}."""
        return self._upsert_fact(category, name, body, importance, owner,
                                 _commit=_commit)

    def _upsert_fact(self, category: str, name: str, body: str,
                     importance: float, owner: str = "",
                     _depth: int = 0, _commit: bool = True) -> dict:
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
                    self._commit_if(_commit)
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
                self._abort()
                if _depth >= 1:
                    raise
                return self._upsert_fact(category, name, body, importance, owner,
                                         _depth + 1, _commit=_commit)
            nid = cur.lastrowid
            self.conn.execute(
                "UPDATE um_facts SET superseded_by=? WHERE id=?", (nid, fid))
            # Рёбра старой версии истекают вместе с ней (граф не отдаёт stale).
            self.conn.execute(
                "UPDATE um_edges SET valid_until=? WHERE fact_id=? AND valid_until=0",
                (now, fid))
            # P4.8: вектор вытесненной версии не нужен (FTS оставляем — на нём
            # держится include_expired). Reopen вернёт факт без вектора до mem_reindex.
            self._vec_delete("um_facts", fid)
            self.conn.execute(
                "DELETE FROM um_vectors WHERE owner_table='um_facts' AND owner_id=?",
                (fid,))
            self._fts_index("um_facts", nid, f"{name} {body}")
            self._commit_if(_commit)
            return {"id": nid, "status": "superseded", "superseded_id": fid}
        try:
            cur = self.conn.execute(
                "INSERT INTO um_facts(owner, category, name, body, importance,"
                " created_at, updated_at, valid_until, superseded_by)"
                " VALUES(?,?,?,?,?,?,?,0,0)",
                (owner, category, name, body, importance, now, now))
        except sqlite3.IntegrityError:
            # backstop: слот занят вне этого процесса — перечитать и свести
            self._abort()
            if _depth >= 1:
                raise
            return self._upsert_fact(category, name, body, importance, owner,
                                     _depth + 1, _commit=_commit)
        fid = cur.lastrowid
        self._fts_index("um_facts", fid, f"{name} {body}")
        self._commit_if(_commit)
        return {"id": fid, "status": "created", "superseded_id": 0}

    @_locked
    def update_fact(self, fid: int, body: str | None = None,
                    importance: float | None = None,
                    valid_until: float | None = None,
                    owner: str = "", _commit: bool = True) -> dict | None:
        """Правка факта по id без потери provenance.

        Сначала valid_until in-place (0 = reopen + возврат рёбер факта), затем
        body/importance. Новое тело = новая версия (supersede), id меняется.
        """
        row = self.conn.execute(
            "SELECT owner, category, name, body, importance, valid_until"
            " FROM um_facts WHERE id=?", (fid,)).fetchone()
        if not row:
            return None
        r_owner, cat, name, cur_body, cur_imp, cur_vu = row
        r_owner = r_owner or ""
        if owner and r_owner != owner:
            return None
        reopened = False
        if valid_until is not None:
            vu = float(valid_until)
            if vu == 0:
                other = self.conn.execute(
                    "SELECT id FROM um_facts WHERE owner=? AND category=? AND name=?"
                    " AND valid_until=0 AND id<>?", (r_owner, cat, name, fid)).fetchone()
                if other:
                    raise ValueError(
                        f"slot already has a live version (id={other[0]});"
                        " supersede it instead of reopening")
                self.conn.execute(
                    "UPDATE um_facts SET valid_until=0, updated_at=? WHERE id=?",
                    (time.time(), fid))
                # ребро вернулось вместе с фактом
                self.conn.execute(
                    "UPDATE um_edges SET valid_until=0"
                    " WHERE fact_id=? AND valid_until>0", (fid,))
                reopened = True
                cur_vu = 0
            else:
                self.conn.execute(
                    "UPDATE um_facts SET valid_until=?, updated_at=? WHERE id=?",
                    (vu, time.time(), fid))
                self.conn.execute(
                    "UPDATE um_edges SET valid_until=?"
                    " WHERE fact_id=? AND valid_until=0", (vu, fid))
                # P4.8: вектор истёкшей версии убираем (FTS — оставляем).
                self._vec_delete("um_facts", fid)
                self.conn.execute(
                    "DELETE FROM um_vectors WHERE owner_table='um_facts' AND owner_id=?",
                    (fid,))
                self._commit_if(_commit)
                if body is not None and body != cur_body:
                    raise ValueError(
                        "cannot edit while expiring; reopen with valid_until=0 first")
                return {"id": fid, "status": "expired", "superseded_id": 0}
            self._commit_if(_commit)
        # здесь факт живой (возможно, только что reopened) — правим body/importance
        if body is not None and body != cur_body:
            if cur_vu > 0:
                raise ValueError(
                    "cannot edit expired fact; reopen with valid_until=0 first")
            return self._upsert_fact(
                cat, name, body,
                importance if importance is not None else cur_imp, r_owner,
                _commit=_commit)
        if importance is not None and abs(importance - cur_imp) > 1e-9:
            self.conn.execute(
                "UPDATE um_facts SET importance=?, updated_at=? WHERE id=?",
                (max(0.0, min(1.0, importance)), time.time(), fid))
            self._commit_if(_commit)
            return {"id": fid, "status": "updated", "superseded_id": 0}
        if reopened:
            return {"id": fid, "status": "reopened", "superseded_id": 0}
        return {"id": fid, "status": "noop", "superseded_id": 0}

    @_locked
    def expire_working_facts(self, owner: str = "", limit: int = 1000) -> dict:
        """Lazy-expire due working slots through the normal fact expiry path.

        FTS history remains available for explicit ``include_expired`` recall;
        the fact/edge vector is removed by ``update_fact``. The bounded batch
        keeps read-path maintenance predictable for large legacy stores.
        """
        now = time.time()
        params: list[object] = [now]
        owner_clause = ""
        if owner:
            owner_clause = " AND owner=?"
            params.append(owner)
        rows = self.conn.execute(
            "SELECT id, valid_until FROM um_facts WHERE category='working'"
            " AND valid_until>0 AND valid_until<=?" + owner_clause
            + " ORDER BY valid_until, id LIMIT ?",
            (*params, limit)).fetchall()
        if not rows:
            return {"expired": 0}
        expired = 0
        with self.transaction():
            for fid, valid_until in rows:
                out = self.update_fact(
                    int(fid), valid_until=float(valid_until), owner=owner,
                    _commit=False)
                if out and out.get("status") == "expired":
                    expired += 1
        return {"expired": expired}

    @_locked
    def update_edge(self, eid: int, valid_until: float, owner: str = "",
                    _commit: bool = True) -> bool:
        """Истечение/reopen ребра (mnemosyne triple_end). Замена = новое ребро."""
        row = self.conn.execute(
            "SELECT owner FROM um_edges WHERE id=?", (eid,)).fetchone()
        if not row:
            return False
        if owner and (row[0] or "") != owner:
            return False
        self.conn.execute("UPDATE um_edges SET valid_until=? WHERE id=?",
                          (float(valid_until), eid))
        self._commit_if(_commit)
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
                    owner: str = "", _commit: bool = True,
                    sources: list[tuple[str, int]] | None = None) -> int:
        source_rows = list(sources or [])
        for source_table, _ in source_rows:
            if source_table not in ("um_messages", "um_summaries"):
                raise ValueError(
                    f"invalid summary source table {source_table!r}")
        cur = self.conn.execute(
            "INSERT INTO um_summaries(session_id, owner, depth, body, covers_from, covers_to, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (session_id, owner, depth, body, covers_from, covers_to, time.time()),
        )
        sid = cur.lastrowid
        for position, (source_table, source_id) in enumerate(source_rows):
            self.conn.execute(
                "INSERT OR IGNORE INTO um_summary_sources"
                "(summary_id, source_table, source_id, position)"
                " VALUES(?,?,?,?)",
                (sid, source_table, int(source_id), position))
        self._fts_index("um_summaries", sid, body)
        tokens = estimate_tokens(body)
        self.bump_tokens(session_id, tokens, owner, _commit=False)
        self.bump_summary_tokens(session_id, tokens, owner, _commit=False)
        self._commit_if(_commit)
        return sid

    @_locked
    def add_vector(self, owner_table: str, owner_id: int,
                   vec: list[float], model: str, owner: str = "",
                   _commit: bool = True,
                   _allow_model_mismatch: bool = False) -> None:
        stored_model = self.meta_get("embedding_model")
        if stored_model is None:
            self.conn.execute(
                "INSERT OR IGNORE INTO um_meta(key, value) VALUES(?, ?)",
                ("embedding_model", model))
            self.conn.execute(
                "INSERT OR IGNORE INTO um_meta(key, value) VALUES(?, ?)",
                ("embedding_dim", str(len(vec))))
        elif not _allow_model_mismatch:
            check_store_dim(len(vec), model, self.meta_get)
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
        self._commit_if(_commit)

    @_locked
    def _fts_index(self, owner_table: str, owner_id: int, body: str) -> None:
        if self.fts:
            self.conn.execute(
                "INSERT INTO um_fts(owner_table, owner_id, body) VALUES(?,?,?)",
                (owner_table, owner_id, body),
            )

    # -- reads ------------------------------------------------------------
    @_locked
    def session_transcript(self, session_id: str, after_id: int = 0,
                           limit: int = 50, owner: str = "") -> list[dict]:
        """Paginate the lossless message log, including archive stubs."""
        q = ("SELECT id, session_id, owner, role, content, created_at, source,"
             " externalized_ref FROM um_messages WHERE session_id=? AND id>?")
        params: list = [session_id, after_id]
        if owner:
            q += " AND owner=?"
            params.append(owner)
        q += " ORDER BY id LIMIT ?"
        params.append(limit)
        keys = ["id", "session_id", "owner", "role", "content", "created_at",
                "source", "externalized_ref"]
        out = []
        for row in self.conn.execute(q, params):
            item = dict(zip(keys, row))
            item["archived"] = bool(item["externalized_ref"])
            item["body"] = None if item["archived"] else item["content"]
            item.pop("content")
            item["archive_ref"] = item.pop("externalized_ref")
            out.append(item)
        return out

    @_locked
    def get_message(self, mid: int, owner: str = "") -> dict | None:
        row = self.conn.execute(
            "SELECT id, session_id, owner, role, content, created_at, source,"
            " externalized_ref FROM um_messages WHERE id=?", (mid,)).fetchone()
        if not row:
            return None
        d = dict(zip(["id", "session_id", "owner", "role", "content",
                      "created_at", "source", "externalized_ref"], row))
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
        # заглушки [archived] не должны попадать в контекст/компакшн
        q += " AND (externalized_ref IS NULL OR externalized_ref='')"
        rows = self.conn.execute(q + " ORDER BY id LIMIT ?", (*params, limit)).fetchall()
        keys = ["id", "session_id", "role", "content", "created_at", "source"]
        return [dict(zip(keys, r)) for r in rows]

    @_locked
    def session_messages_tail(self, session_id: str, limit: int = 50,
                              owner: str = "", after_id: int = 0) -> list[dict]:
        """D15: свежие `limit` сообщений (ASC на выходе) одним DESC/LIMIT-запросом.

        Бюджет/хвост считаются по токенам в Python, поэтому для сборки контекста
        достаточно прочитать не всю сессию, а только возможный хвост."""
        q = ("SELECT id, session_id, role, content, created_at, source FROM um_messages"
             " WHERE session_id=? AND id>?")
        params: list = [session_id, after_id]
        if owner:
            q += " AND owner=?"
            params.append(owner)
        q += " AND (externalized_ref IS NULL OR externalized_ref='')"
        rows = self.conn.execute(q + " ORDER BY id DESC LIMIT ?",
                                 (*params, limit)).fetchall()
        rows.reverse()  # на выходе — по возрастанию id, как session_messages
        keys = ["id", "session_id", "role", "content", "created_at", "source"]
        return [dict(zip(keys, r)) for r in rows]

    @_locked
    def fts_search(self, query: str, scope: str = "all",
                   session_id: str = "", limit: int = 20,
                   owner: str = "",
                   include_expired: bool = False,
                   as_of: float | None = None,
                   source: str = "") -> list[Hit]:
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
        if source:
            if scope == "facts":
                return []
            tables = ["um_messages"]
        if self.fts and max(len(t) for t in terms) >= 3:
            match = " OR ".join(f'"{t}"' for t in terms[:10] if len(t) >= 3)
            # The FTS table stores only (owner_table, owner_id, body), so scope
            # filters must be joined back to the parent rows before LIMIT.
            # Filtering after the global FTS limit lets unrelated sessions or
            # owners consume the whole candidate set.
            alias_by_table = {
                "um_messages": "m", "um_summaries": "s",
                "um_facts": "fct", "um_edges": "e",
            }
            joins = []
            scoped = []
            scope_params: list = []
            for table in tables:
                alias = alias_by_table[table]
                joins.append(
                    f" LEFT JOIN {table} AS {alias}"
                    f" ON f.owner_table='{table}' AND f.owner_id={alias}.id"
                )
                checks = [f"{alias}.id IS NOT NULL"]
                if owner:
                    checks.append(f"{alias}.owner=?")
                    scope_params.append(owner)
                if scope == "session" and table in (
                        "um_messages", "um_summaries", "um_edges"):
                    checks.append(f"{alias}.session_id=?")
                    scope_params.append(session_id)
                if source and table == "um_messages":
                    checks.append(f"{alias}.source=?")
                    scope_params.append(source)
                if table in ("um_facts", "um_edges"):
                    if as_of is not None:
                        checks.extend([
                            f"{alias}.created_at<=?",
                            f"({alias}.valid_until=0 OR {alias}.valid_until>?)",
                        ])
                        scope_params.extend([as_of, as_of])
                    elif not include_expired:
                        checks.append(
                            f"({alias}.valid_until=0 OR {alias}.valid_until>?)"
                        )
                        scope_params.append(time.time())
                scoped.append(
                    f"(f.owner_table='{table}' AND {' AND '.join(checks)})"
                )
            ph = ",".join("?" * len(tables))
            q = ("SELECT f.owner_table, f.owner_id,"
                 " snippet(um_fts, 2, '[', ']', '…', 8) FROM um_fts AS f"
                 + "".join(joins)
                 + f" WHERE um_fts MATCH ? AND f.owner_table IN ({ph})"
                 + " AND (" + " OR ".join(scoped) + ")"
                 " ORDER BY rank LIMIT ?")
            try:
                rows = self.conn.execute(
                    q, (match, *tables, *scope_params, limit * 3)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            cand = [(ot, oid) for ot, oid, _ in rows if ot in tables]
            snips = {(ot, oid): sn for ot, oid, sn in rows if ot in tables}
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
                hits.append(Hit(ot, oid, body, 1.0, sid,
                                snippet=snips.get((ot, oid), "")))
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
            if source and ot == "um_messages":
                cond = f"(source=?) AND ({cond})"
                params = [source] + params
            if ot in ("um_messages", "um_summaries") and scope == "session":
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
                if ot in ("um_messages", "um_summaries"):
                    s = self.conn.execute(
                        f"SELECT session_id FROM {ot} WHERE id=?", (oid,)).fetchone()
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
    def fact_row(self, fid: int) -> tuple[str, str, str] | None:
        """(owner, name, body) факта — для переэмбеддинга новых/reopen версий (A2)."""
        r = self.conn.execute(
            "SELECT owner, name, body FROM um_facts WHERE id=?", (fid,)).fetchone()
        return (r[0] or "", r[1], r[2]) if r else None

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
    def delete_fact(self, fid: int, owner: str = "",
                    _commit: bool = True) -> bool:
        if owner:
            row = self.conn.execute(
                "SELECT owner FROM um_facts WHERE id=?", (fid,)).fetchone()
            if not row or (row[0] or "") != owner:
                return False
        cur = self.conn.execute("DELETE FROM um_facts WHERE id=?", (fid,))
        self.conn.execute(
            "DELETE FROM um_annotations WHERE target_table='um_facts' AND target_id=?",
            (fid,))
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
                "DELETE FROM um_annotations WHERE target_table='um_edges' AND target_id=?",
                (eid,))
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
        self._commit_if(_commit)
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
    def ref_details(self, owner_table: str, owner_id: int,
                    owner: str = "") -> dict | None:
        """Read one object with metadata, vector state, and direct links."""
        if owner_table not in ("um_messages", "um_summaries", "um_facts", "um_edges"):
            return None
        ref = (owner_table, owner_id)
        if owner and self.owners_for([ref]).get(ref, "") != owner:
            return None
        body, session_id = self._body_of(owner_table, owner_id)
        if body is None:
            return None
        if owner_table == "um_messages":
            row = self.conn.execute(
                "SELECT session_id, owner, role, created_at, source,"
                " externalized_ref, conversation_id, source_order, source_ref,"
                " metadata_json FROM um_messages WHERE id=?", (owner_id,)
            ).fetchone()
            metadata = dict(zip(
                ["session_id", "owner", "role", "created_at", "source",
                 "externalized_ref", "conversation_id", "source_order", "source_ref",
                 "metadata_json"], row or ()))
        elif owner_table == "um_summaries":
            row = self.conn.execute(
                "SELECT session_id, owner, depth, covers_from, covers_to,"
                " superseded_by, created_at, metadata_json FROM um_summaries WHERE id=?",
                (owner_id,)
            ).fetchone()
            metadata = dict(zip(
                ["session_id", "owner", "depth", "covers_from", "covers_to",
                 "superseded_by", "created_at", "metadata_json"], row or ()))
        elif owner_table == "um_facts":
            row = self.conn.execute(
                "SELECT owner, category, name, importance, created_at, updated_at,"
                " valid_until, superseded_by, metadata_json, confidence, veracity,"
                " source_ref FROM um_facts WHERE id=?", (owner_id,)
            ).fetchone()
            metadata = dict(zip(
                ["owner", "category", "name", "importance", "created_at",
                 "updated_at", "valid_until", "superseded_by", "metadata_json",
                 "confidence", "veracity", "source_ref"], row or ()))
        else:
            row = self.conn.execute(
                """SELECT e.subject_id, e.predicate, e.object_id, e.session_id,
                          e.owner, e.fact_id, e.created_at, e.valid_until,
                          e.metadata_json, e.confidence, e.veracity, e.source_ref,
                          s.display, o.display
                   FROM um_edges e
                   JOIN um_entities s ON s.id=e.subject_id
                   JOIN um_entities o ON o.id=e.object_id
                   WHERE e.id=?""", (owner_id,)
            ).fetchone()
            metadata = dict(zip(
                ["subject_id", "predicate", "object_id", "session_id", "owner",
                 "fact_id", "created_at", "valid_until", "metadata_json",
                 "confidence", "veracity", "source_ref", "subject", "object"],
                row or ()))
        vector = {"present": False}
        vrow = self.conn.execute(
            "SELECT model, embedding FROM um_vectors"
            " WHERE owner_table=? AND owner_id=?", ref).fetchone()
        if vrow:
            try:
                dim = len(unpack_vector(vrow[1]))
            except Exception:
                dim = 0
            vector = {"present": True, "model": vrow[0], "dim": dim}
        links = []
        for row in self.conn.execute(
                """SELECT src_table, src_id, dst_table, dst_id, rel, weight,
                          owner, valid_until
                   FROM um_links
                   WHERE (src_table=? AND src_id=?)
                      OR (dst_table=? AND dst_id=?)
                   ORDER BY id""",
                (owner_table, owner_id, owner_table, owner_id)):
            links.append(dict(zip(
                ["src_table", "src_id", "dst_table", "dst_id", "rel", "weight",
                 "owner", "valid_until"], row)))
        annotations = self.annotations_for(owner_table, owner_id, owner=owner or "")
        return {"body": body, "session_id": session_id, "metadata": metadata,
                "vector": vector, "links": links, "annotations": annotations}


    @_locked
    def summary_lineage(self, summary_id: int, owner: str = "") -> dict | None:
        """Return direct source refs for one summary, preserving order."""
        if not self.node_ok("um_summaries", int(summary_id), owner):
            return None
        refs = []
        for source_table, source_id, position in self.conn.execute(
                "SELECT source_table, source_id, position FROM um_summary_sources"
                " WHERE summary_id=? ORDER BY position, source_table, source_id",
                (int(summary_id),)):
            refs.append({
                "kind": ("message" if source_table == "um_messages" else "summary"),
                "table": source_table,
                "id": int(source_id),
                "position": int(position),
            })
        return {"summary_id": int(summary_id), "sources": refs}

    @_locked
    def fact_slots(self, refs: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
        """(owner, category, name, body) для um_facts refs — для conflict-detect."""
        ids = [oid for ot, oid in refs if ot == "um_facts"]
        out: dict[tuple[str, int], dict] = {}
        if not ids:
            return out
        ph = ",".join("?" * len(ids))
        for oid, owner, cat, name, body in self.conn.execute(
                f"SELECT id, owner, category, name, body FROM um_facts"
                f" WHERE id IN ({ph})", ids):
            out[("um_facts", oid)] = {"owner": owner or "", "category": cat,
                                      "name": name, "body": body}
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
    def sources_for(self, refs: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
        """Batch message source values for recall filtering."""
        ids = [oid for ot, oid in refs if ot == "um_messages"]
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        return {
            ("um_messages", oid): source or ""
            for oid, source in self.conn.execute(
                f"SELECT id, source FROM um_messages WHERE id IN ({ph})", ids)
        }

    @_locked
    def importance_for(self, refs: list[tuple[str, int]]) -> dict[tuple[str, int], float]:
        """Batch fact importance values for optional recall ranking."""
        ids = [oid for ot, oid in refs if ot == "um_facts"]
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        return {
            ("um_facts", oid): max(0.0, min(1.0, float(importance)))
            for oid, importance in self.conn.execute(
                f"SELECT id, importance FROM um_facts WHERE id IN ({ph})", ids)
        }

    @_locked
    def recent(self, start_ts: float, end_ts: float, session_id: str = "",
               owner: str = "", limit: int = 20,
               before_ts: float = 0.0, before_id: int = 0,
               before_kind: str = "") -> list[dict]:
        """Temporal выборка поверх messages+summaries: [start, end), свежие first.

        D14-пагинация: (before_ts, before_id, before_kind) — эксклюзивный курсор
        «строго старше». kind — тайбрейкер полного (created_at, id)-тия между
        таблицами: глобальный порядок (created_at DESC, id DESC, kind-rank DESC;
        summaries раньше messages), курсор — «строго после позиции»
        (own_rank < brank). before_kind="" (legacy) → rank 0: вся (ts, id)-
        группа исключена — поведение до тайбрейкера, бит-в-бит."""
        out: list[dict] = []
        cur_m = cur_s = ""
        curs: list = []
        if before_ts or before_id:
            brank = _KIND_RANK.get(before_kind, 0)
            tmpl = (" AND (created_at < ? OR (created_at = ? AND (id < ?"
                    " OR (id = ? AND {rk} < ?))))")
            cur_m = tmpl.format(rk=_KIND_RANK["um_messages"])
            cur_s = tmpl.format(rk=_KIND_RANK["um_summaries"])
            curs = [before_ts, before_ts, before_id, before_id, brank]
        mq = ("SELECT id, session_id, content, created_at FROM um_messages"
              " WHERE created_at >= ? AND created_at < ?")
        mp: list = [start_ts, end_ts]
        if session_id:
            mq += " AND session_id=?"
            mp.append(session_id)
        if owner:
            mq += " AND owner=?"
            mp.append(owner)
        mq += " AND (externalized_ref IS NULL OR externalized_ref='')"
        for mid, sid, body, ts in self.conn.execute(
                mq + cur_m + " ORDER BY created_at DESC, id DESC LIMIT ?",
                (*mp, *curs, limit)):
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
                sq + cur_s + " ORDER BY created_at DESC, id DESC LIMIT ?",
                (*sp, *curs, limit)):
            out.append({"kind": "um_summaries", "id": sid_, "session_id": ssid,
                        "body": body, "created_at": ts})
        out.sort(key=lambda r: (-r["created_at"], -r["id"],
                                 -_KIND_RANK[r["kind"]]))
        return out[:limit]

    @_locked
    def db_size_bytes(self) -> int:
        pc = self.conn.execute("PRAGMA page_count").fetchone()[0]
        ps = self.conn.execute("PRAGMA page_size").fetchone()[0]
        return int(pc) * int(ps)

    @_locked
    def oldest_messages(self, limit: int = 500,
                        before_ts: float = 0.0) -> list[dict]:
        """Старейшие НЕархивированные сообщения (для выноса в холодный архив)."""
        q = ("SELECT id, session_id, owner, role, content, created_at, source"
             " FROM um_messages"
             " WHERE (externalized_ref IS NULL OR externalized_ref='')")
        params: list = []
        if before_ts:
            q += " AND created_at < ?"
            params.append(before_ts)
        q += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(limit)
        keys = ["id", "session_id", "owner", "role", "content", "created_at", "source"]
        return [dict(zip(keys, r)) for r in self.conn.execute(q, params)]

    @_locked
    def message_vector(self, mid: int) -> tuple[bytes, str] | None:
        """Сырой blob вектора + модель (копируется в архив без распаковки)."""
        r = self.conn.execute(
            "SELECT embedding, model FROM um_vectors"
            " WHERE owner_table='um_messages' AND owner_id=?", (mid,)).fetchone()
        return (r[0], r[1]) if r else None

    @_locked
    def external_ref(self, mid: int) -> str | None:
        r = self.conn.execute(
            "SELECT externalized_ref FROM um_messages WHERE id=?", (mid,)).fetchone()
        return (r[0] or None) if r else None

    @_locked
    def mark_archived(self, mid: int, ref: str) -> None:
        """Текст -> заглушка + externalized_ref; вектор и FTS из горячей удаляются (a2)."""
        self.conn.execute(
            "UPDATE um_messages SET content=?, externalized_ref=? WHERE id=?",
            ("[archived]", ref, mid))
        self._vec_delete("um_messages", mid)
        self.conn.execute(
            "DELETE FROM um_vectors WHERE owner_table='um_messages' AND owner_id=?", (mid,))
        if self.fts:
            self.conn.execute(
                "DELETE FROM um_fts WHERE owner_table='um_messages' AND owner_id=?", (mid,))
        # Archiving changes the raw-message set. Force a one-time pressure
        # rebuild instead of leaving stale per-session counters behind.
        self.conn.execute(
            "DELETE FROM um_meta WHERE key LIKE 'tokens:%'"
            " OR key LIKE 'raw_tokens:%' OR key LIKE 'summary_tokens:%'")
        self.conn.commit()
        self._restrict_artifacts()

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
        self._restrict_artifacts()
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
            "DELETE FROM um_annotations WHERE target_table='um_edges' AND target_id=?",
            (eid,))
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
    def delete_edge(self, eid: int, owner: str = "",
                    _commit: bool = True) -> bool:
        """Прямое удаление ребра — закрывает дыру бессмертных fact_id=0 (#3)."""
        row = self.conn.execute(
            "SELECT owner FROM um_edges WHERE id=?", (eid,)).fetchone()
        if not row:
            return False
        if owner and (row[0] or "") != owner:
            return False
        self._drop_edge_rows(eid)
        self._prune_orphan_entities()
        self._commit_if(_commit)
        return True

    @_locked
    def delete_entity(self, name: str, owner: str = "",
                      _commit: bool = True) -> bool:
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
        self._commit_if(_commit)
        return True

    # -- graph ----------------------------------------------------------
    @_locked
    def add_entity(self, name: str, owner: str = "",
                   _commit: bool = True) -> int:
        import time as _t
        key = name.strip().lower()
        row = self.conn.execute(
            "SELECT id FROM um_entities WHERE name=? AND owner=?", (key, owner)).fetchone()
        if row:
            return row[0]
        cur = self.conn.execute(
            "INSERT INTO um_entities(name, display, created_at, owner) VALUES(?,?,?,?)",
            (key, name.strip(), _t.time(), owner))
        self._commit_if(_commit)
        return cur.lastrowid

    @_locked
    def add_edge(self, subject: str, predicate: str, obj: str,
                 session_id: str = "", fact_id: int = 0, owner: str = "",
                 _commit: bool = True) -> int:
        import time as _t
        sid = self.add_entity(subject, owner, _commit=False)
        oid = self.add_entity(obj, owner, _commit=False)
        cur = self.conn.execute(
            "INSERT INTO um_edges(subject_id, predicate, object_id, session_id,"
            " fact_id, created_at, owner) VALUES(?,?,?,?,?,?,?)",
            (sid, predicate.strip().lower(), oid, session_id, fact_id, _t.time(), owner))
        eid = cur.lastrowid
        self._fts_index("um_edges", eid, f"{subject} {predicate} {obj}")
        self._commit_if(_commit)
        return eid

    @_locked
    def link(self, src_table: str, src_id: int, dst_table: str, dst_id: int,
             rel: str, weight: float = 1.0, session_id: str = "",
             owner: str = "", _commit: bool = True) -> dict:
        """Типизированная связь (ADR-001). D4: оба конца существуют и owner-совпадают.
        D3: повторный вызов для живого (src,dst,rel,owner) — no-op, отдаёт тот же id.
        session_id наследуется от вызова (у концов сессии могут различаться)."""
        import time as _t
        if src_table not in LINK_TABLES or dst_table not in LINK_TABLES:
            raise ValueError(
                f"bad endpoint table: {src_table!r}/{dst_table!r}; "
                f"ожидаю {list(LINK_TABLES)}")
        if rel not in LINK_RELS:
            raise ValueError(f"unknown rel {rel!r}: {list(LINK_RELS)}")
        if weight < 0:
            raise ValueError("weight must be >= 0")
        if src_table == dst_table and src_id == dst_id:
            raise ValueError("self-link is not allowed (src == dst)")
        for tbl, oid in ((src_table, src_id), (dst_table, dst_id)):
            row = self.conn.execute(
                f"SELECT owner FROM {tbl} WHERE id=?", (oid,)).fetchone()
            if row is None:
                raise ValueError(f"endpoint {tbl}:{oid} not found")
            if (row[0] or "") != owner:
                raise ValueError(f"endpoint {tbl}:{oid} owner mismatch")
        key = (src_table, src_id, dst_table, dst_id, rel, owner)
        row = self.conn.execute(
            "SELECT id FROM um_links WHERE src_table=? AND src_id=? AND dst_table=?"
            " AND dst_id=? AND rel=? AND owner=? AND valid_until=0", key).fetchone()
        if row:
            return {"id": row[0], "created": False, "rel": rel,
                    "src": f"{src_table}:{src_id}", "dst": f"{dst_table}:{dst_id}"}
        try:
            cur = self.conn.execute(
                "INSERT INTO um_links(src_table, src_id, dst_table, dst_id, rel,"
                " weight, owner, session_id, created_at, valid_until)"
                " VALUES(?,?,?,?,?,?,?,?,?,0)",
                (src_table, src_id, dst_table, dst_id, rel, weight, owner,
                 session_id, _t.time()))
            lid = cur.lastrowid
            self._commit_if(_commit)
        except sqlite3.IntegrityError:
            # гонка: связь создали между SELECT и INSERT
            row = self.conn.execute(
                "SELECT id FROM um_links WHERE src_table=? AND src_id=? AND dst_table=?"
                " AND dst_id=? AND rel=? AND owner=? AND valid_until=0", key).fetchone()
            if not row:
                raise
            return {"id": row[0], "created": False, "rel": rel,
                    "src": f"{src_table}:{src_id}", "dst": f"{dst_table}:{dst_id}"}
        return {"id": lid, "created": True, "rel": rel,
                "src": f"{src_table}:{src_id}", "dst": f"{dst_table}:{dst_id}"}

    @_locked
    def update_link(self, lid: int, valid_until: float, owner: str = "",
                    _commit: bool = True) -> bool:
        """Истечение/reopen связи (зеркало update_edge, ADR-001 D6).
        Замена связи = новая связь; rel не редактируется."""
        row = self.conn.execute(
            "SELECT owner FROM um_links WHERE id=?", (lid,)).fetchone()
        if not row:
            return False
        if owner and (row[0] or "") != owner:
            return False
        self.conn.execute("UPDATE um_links SET valid_until=? WHERE id=?",
                          (float(valid_until), lid))
        self._commit_if(_commit)
        return True

    @_locked
    def delete_link(self, lid: int, owner: str = "",
                    _commit: bool = True) -> bool:
        """Жёсткое удаление связи (симметрично GDPR-hatch фактов/рёбер)."""
        row = self.conn.execute(
            "SELECT owner FROM um_links WHERE id=?", (lid,)).fetchone()
        if not row:
            return False
        if owner and (row[0] or "") != owner:
            return False
        self.conn.execute("DELETE FROM um_links WHERE id=?", (lid,))
        self._commit_if(_commit)
        return True

    @_locked
    def annotate(self, target_table: str, target_id: int, kind: str,
                 value: str = "", source: str = "",
                 confidence: float = 1.0, owner: str = "",
                 _commit: bool = True) -> dict:
        """P2.2: пометка поверх ref. Повтор тех же (target,kind,value,owner) — no-op.

        Цель обязана существовать и принадлежать owner (legacy '' видит всё).
        Metadata-only: FTS/вектора не трогаем, recall не меняется.
        """
        import time as _t
        target_table = {"message": "um_messages", "fact": "um_facts",
                        "summary": "um_summaries", "edge": "um_edges"}.get(
                            target_table, target_table)
        if target_table not in ANNOTATION_TABLES:
            raise ValueError(
                f"bad target table {target_table!r}; ожидаю {list(ANNOTATION_TABLES)}")
        if kind not in ANNOTATION_KINDS:
            raise ValueError(f"unknown kind {kind!r}: {list(ANNOTATION_KINDS)}")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            raise ValueError("confidence must be a number in [0, 1]")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        row = self.conn.execute(
            f"SELECT owner FROM {target_table} WHERE id=?", (int(target_id),)).fetchone()
        if row is None:
            raise ValueError(f"target {target_table}:{target_id} not found")
        if (row[0] or "") != owner:
            raise ValueError(f"target {target_table}:{target_id} owner mismatch")
        key = (target_table, int(target_id), kind, value or "", owner)
        ex = self.conn.execute(
            "SELECT id FROM um_annotations WHERE target_table=? AND target_id=?"
            " AND kind=? AND value=? AND owner=?", key).fetchone()
        if ex:
            return {"id": ex[0], "created": False}
        try:
            cur = self.conn.execute(
                "INSERT INTO um_annotations(target_table, target_id, kind, value,"
                " source, confidence, owner, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (target_table, int(target_id), kind, value or "", source or "",
                 confidence, owner, _t.time()))
            aid = cur.lastrowid
            self._commit_if(_commit)
        except sqlite3.IntegrityError:
            ex = self.conn.execute(
                "SELECT id FROM um_annotations WHERE target_table=? AND target_id=?"
                " AND kind=? AND value=? AND owner=?", key).fetchone()
            if not ex:
                raise
            return {"id": ex[0], "created": False}
        return {"id": aid, "created": True}

    @_locked
    def annotations_for(self, target_table: str, target_id: int,
                        owner: str = "") -> list[dict]:
        """Пометки цели. owner задан — только его; '' — legacy без фильтра."""
        target_table = {"message": "um_messages", "fact": "um_facts",
                        "summary": "um_summaries", "edge": "um_edges"}.get(
                            target_table, target_table)
        if target_table not in ANNOTATION_TABLES:
            return []
        sql = ("SELECT id, kind, value, source, confidence, owner, created_at"
               " FROM um_annotations WHERE target_table=? AND target_id=?")
        params: list[object] = [target_table, int(target_id)]
        if owner:
            sql += " AND owner=?"
            params.append(owner)
        sql += " ORDER BY id"
        return [dict(zip(
            ["id", "kind", "value", "source", "confidence", "owner", "created_at"],
            row)) for row in self.conn.execute(sql, params)]

    @_locked
    def delete_annotation(self, aid: int, owner: str = "",
                          _commit: bool = True) -> bool:
        """Жёсткое удаление пометки (GDPR-hatch)."""
        row = self.conn.execute(
            "SELECT owner FROM um_annotations WHERE id=?", (int(aid),)).fetchone()
        if not row:
            return False
        if owner and (row[0] or "") != owner:
            return False
        self.conn.execute("DELETE FROM um_annotations WHERE id=?", (int(aid),))
        self._commit_if(_commit)
        return True

    @_locked
    def node_ok(self, table: str, oid: int, owner: str = "",
                include_expired: bool = False, as_of: float | None = None,
                session_id: str = "") -> bool:
        """Виден ли узел (table,id) при owner/session/liveness-фильтрах.
        Архивные заглушки сообщений скрыты, как в остальных руках recall."""
        if table == "um_messages":
            row = self.conn.execute(
                "SELECT owner, externalized_ref, session_id"
                " FROM um_messages WHERE id=?", (oid,)).fetchone()
            if not row or row[1]:
                return False
            if session_id and row[2] != session_id:
                return False
            return not owner or (row[0] or "") == owner
        if table == "um_summaries":
            row = self.conn.execute(
                "SELECT owner, session_id FROM um_summaries WHERE id=?",
                (oid,)).fetchone()
            return (bool(row)
                    and (not session_id or row[1] == session_id)
                    and (not owner or (row[0] or "") == owner))
        if table == "um_facts":
            if session_id:
                return False  # facts не имеют session dimension
            row = self.conn.execute(
                "SELECT owner, created_at, valid_until FROM um_facts WHERE id=?",
                (oid,)).fetchone()
            if not row:
                return False
            if owner and (row[0] or "") != owner:
                return False
            ca, vu = row[1] or 0.0, row[2] or 0.0
            if as_of is not None:
                return ca <= as_of and (vu == 0 or vu > as_of)
            if include_expired:
                return True
            return vu == 0 or vu > time.time()
        if table == "um_edges":
            row = self.conn.execute(
                "SELECT owner, created_at, valid_until, session_id"
                " FROM um_edges WHERE id=?", (oid,)).fetchone()
            if not row:
                return False
            if owner and (row[0] or "") != owner:
                return False
            if session_id and row[3] != session_id:
                return False
            ca, vu = row[1] or 0.0, row[2] or 0.0
            if as_of is not None:
                return ca <= as_of and (vu == 0 or vu > as_of)
            if include_expired:
                return True
            return vu == 0 or vu > time.time()
        return False

    @_locked
    def link_neighbors(self, table: str, oid: int, owner: str = "",
                       include_expired: bool = False, as_of: float | None = None,
                       rel: str = "", min_weight: float = 0.0,
                       session_id: str = "", limit: int = 20) -> list[dict]:
        """Соседи узла по um_links в ОБЕ стороны (ADR-001 D5). Не более limit
        на направление. Liveness линка проверяется здесь; узла-назначения — вызывающим."""
        now = time.time()
        out: list[dict] = []
        dirs = (("out", "src_table=? AND src_id=?",
                 "id, dst_table, dst_id, rel, weight, session_id, created_at, valid_until"),
                ("in", "dst_table=? AND dst_id=?",
                 "id, src_table, src_id, rel, weight, session_id, created_at, valid_until"))
        for direction, where, cols in dirs:
            q = f"SELECT {cols} FROM um_links WHERE {where}"
            p: list = [table, oid]
            if owner:
                q += " AND owner=?"
                p.append(owner)
            if rel:
                q += " AND rel=?"
                p.append(rel)
            if min_weight:
                q += " AND weight>=?"
                p.append(float(min_weight))
            if session_id:
                q += " AND session_id=?"
                p.append(session_id)
            q += " ORDER BY id"
            got = 0
            for lid, nt, nid, rl, w, sid, ca, vu in self.conn.execute(q, p):
                if as_of is not None:
                    if not (ca <= as_of and (vu == 0 or vu > as_of)):
                        continue
                elif not include_expired and not (vu == 0 or vu > now):
                    continue
                if direction == "out":
                    src_table, src_id, dst_table, dst_id = table, oid, nt, nid
                else:
                    src_table, src_id, dst_table, dst_id = nt, nid, table, oid
                out.append({"link_id": lid, "table": nt, "id": nid, "rel": rl, "weight": w,
                            "session_id": sid or "", "created_at": ca,
                            "src_table": src_table, "src_id": src_id,
                            "dst_table": dst_table, "dst_id": dst_id})
                got += 1
                if got >= limit:
                    break
        return out

    @_locked
    def edge_steps_of_entity(self, eid: int, owner: str = "",
                             include_expired: bool = False,
                             as_of: float | None = None, session_id: str = "",
                             predicate: str = "", limit: int = 100) -> list[dict]:
        """Шаг BFS по entity-графу: рёбра сущности + id второго конца."""
        now = time.time()
        q = ("SELECT e.id, e.subject_id, e.object_id, e.predicate,"
             " e.session_id, e.created_at, e.valid_until, e.fact_id,"
             " s.name, o.name"
             " FROM um_edges e"
             " JOIN um_entities s ON s.id=e.subject_id"
             " JOIN um_entities o ON o.id=e.object_id"
             " WHERE (e.subject_id=? OR e.object_id=?)")
        p: list = [eid, eid]
        if owner:
            q += " AND e.owner=?"
            p.append(owner)
        if session_id:
            q += " AND e.session_id=?"
            p.append(session_id)
        if predicate:
            q += " AND e.predicate=?"
            p.append(predicate)
        q += " ORDER BY e.id LIMIT ?"
        p.append(limit)
        out = []
        for eid2, sub_id, obj_id, pred, sid, ca, vu, fid, sub, obj \
                in self.conn.execute(q, p):
            if as_of is not None:
                if not (ca <= as_of and (vu == 0 or vu > as_of)):
                    continue
            elif not include_expired and not (vu == 0 or vu > now):
                continue
            out.append({"edge_id": eid2, "other_id": obj_id if sub_id == eid else sub_id,
                        "predicate": pred, "session_id": sid or "",
                        "created_at": ca, "fact_id": fid,
                        "subject": sub, "object": obj})
        return out

    @_locked
    def entity_ids_for_fact(self, fid: int, owner: str = "",
                            limit: int = 20) -> list[dict]:
        """Мост fact→entities через um_edges.fact_id (ADR-001 D5)."""
        q = "SELECT subject_id, object_id, predicate FROM um_edges WHERE fact_id=?"
        p: list = [fid]
        if owner:
            q += " AND owner=?"
            p.append(owner)
        q += " LIMIT ?"
        p.append(limit)
        return [{"subject_id": s, "object_id": o, "predicate": pr}
                for s, o, pr in self.conn.execute(q, p)]

    @_locked
    def match_entity_ids(self, terms: list[str], limit: int = 5,
                         owner: str = "") -> list[int]:
        """Как match_entities, но возвращает id (нужно для узлов (um_entities,id))."""
        out: list[int] = []
        for t in terms[:8]:
            esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            q = "SELECT id FROM um_entities WHERE name LIKE ? ESCAPE '\\'"
            p: list = [f"%{esc}%"]
            if owner:
                q += " AND owner=?"
                p.append(owner)
            for (eid,) in self.conn.execute(q + " LIMIT ?", (*p, limit)):
                if eid not in out:
                    out.append(eid)
        return out[:limit]

    @_locked
    def graph_edges(self, subject: str = "", predicate: str = "",
                    object_name: str = "", session_id: str = "",
                    owner: str = "", include_expired: bool = False,
                    as_of: float | None = None, limit: int = 100) -> list[dict]:
        """Exact-filter entity edges in deterministic id order."""
        now = time.time()
        q = ("SELECT e.id, e.subject_id, e.object_id, e.predicate,"
             " e.session_id, e.owner, e.created_at, e.valid_until, e.fact_id,"
             " s.name, o.name"
             " FROM um_edges e"
             " JOIN um_entities s ON s.id=e.subject_id"
             " JOIN um_entities o ON o.id=e.object_id"
             " WHERE 1=1")
        params: list = []
        if subject:
            q += " AND s.name=?"
            params.append(subject.strip().lower())
        if predicate:
            q += " AND e.predicate=?"
            params.append(predicate.strip().lower())
        if object_name:
            q += " AND o.name=?"
            params.append(object_name.strip().lower())
        if session_id:
            q += " AND e.session_id=?"
            params.append(session_id)
        if owner:
            q += " AND e.owner=?"
            params.append(owner)
        if as_of is not None:
            q += " AND e.created_at<=? AND (e.valid_until=0 OR e.valid_until>?)"
            params.extend([as_of, as_of])
        elif not include_expired:
            q += " AND (e.valid_until=0 OR e.valid_until>?)"
            params.append(now)
        q += " ORDER BY e.id LIMIT ?"
        params.append(int(limit))
        keys = ["id", "subject_id", "object_id", "predicate", "session_id",
                "owner", "created_at", "valid_until", "fact_id", "subject", "object"]
        return [dict(zip(keys, row)) for row in self.conn.execute(q, params)]

    @_locked
    def graph_query(self, subject: str = "", predicate: str = "",
                    object: str = "", session_id: str = "",
                    owner: str = "", rel: str = "", min_weight: float = 0.0,
                    max_hops: int = 1, include_expired: bool = False,
                    as_of: float | None = None, limit: int = 100) -> dict:
        """Bounded deterministic traversal of entity edges and typed links.

        Direct entity edges use exact subject/predicate/object filters. Typed
        links use rel/min_weight; both edge and link liveness honor as_of or
        include_expired. Results are separate lists and never cross a non-empty
        owner boundary.
        """
        if max_hops < 1:
            raise ValueError("max_hops must be >= 1")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if min_weight < 0:
            raise ValueError("min_weight must be >= 0")
        limit = min(int(limit), 500)
        max_hops = int(max_hops)
        rel_filter = rel.strip().lower()
        base = self.graph_edges(
            subject=subject, predicate=predicate, object_name=object,
            session_id=session_id, owner=owner, include_expired=include_expired,
            as_of=as_of, limit=limit + 1)
        truncated = len(base) > limit
        base = base[:limit]
        edges: dict[int, dict] = {}
        links: dict[int, dict] = {}
        total = 0
        queue: list[tuple[str, int, int]] = []
        visited: set[tuple[str, int]] = set()

        def enqueue(table: str, oid: int, depth: int) -> None:
            key = (table, int(oid))
            if key not in visited and len(visited) < limit * 20:
                visited.add(key)
                queue.append((table, int(oid), depth))

        def add_edge(row: dict, depth: int) -> bool:
            nonlocal total
            eid = int(row["id"])
            if eid in edges or total >= limit:
                return False
            edges[eid] = {
                "kind": "um_edges", "id": eid,
                "subject": row["subject"], "predicate": row["predicate"],
                "object": row["object"], "session_id": row["session_id"] or "",
                "depth": depth,
            }
            total += 1
            return True

        def add_link(row: dict, depth: int) -> bool:
            nonlocal total
            lid = int(row["link_id"])
            if lid in links or total >= limit:
                return False
            links[lid] = {
                "kind": "um_links", "id": lid,
                "src": {"table": row["src_table"], "id": int(row["src_id"])},
                "dst": {"table": row["dst_table"], "id": int(row["dst_id"])},
                "rel": row["rel"], "weight": float(row["weight"] or 0.0),
                "session_id": row["session_id"] or "", "depth": depth,
            }
            total += 1
            return True

        for row in base:
            if not add_edge(row, 1):
                break
            enqueue("um_edges", int(row["id"]), 0)
            if int(row["fact_id"] or 0):
                enqueue("um_facts", int(row["fact_id"]), 0)
            enqueue("um_entities", int(row["subject_id"]), 1)
            enqueue("um_entities", int(row["object_id"]), 1)

        head = 0
        while head < len(queue) and total < limit:
            table, oid, depth = queue[head]
            head += 1
            if depth >= max_hops:
                continue
            if table != "um_entities" and not self.node_ok(
                    table, oid, owner=owner, include_expired=include_expired,
                    as_of=as_of, session_id=session_id):
                continue
            if table == "um_entities":
                for step in self.edge_steps_of_entity(
                        oid, owner=owner, include_expired=include_expired,
                        as_of=as_of, session_id=session_id,
                        predicate=predicate.strip().lower(), limit=limit):
                    edge_row = {
                        "id": step["edge_id"], "subject": step["subject"],
                        "predicate": step["predicate"], "object": step["object"],
                        "session_id": step["session_id"],
                    }
                    edge_depth = depth + 1
                    if add_edge(edge_row, edge_depth):
                        enqueue("um_entities", int(step["other_id"]), edge_depth)
                    if int(step.get("fact_id") or 0):
                        enqueue("um_facts", int(step["fact_id"]), edge_depth)
                continue
            if table == "um_facts":
                for bridge in self.entity_ids_for_fact(oid, owner=owner,
                                                         limit=limit):
                    enqueue("um_entities", int(bridge["subject_id"]), depth + 1)
                    enqueue("um_entities", int(bridge["object_id"]), depth + 1)
            for neighbor in self.link_neighbors(
                    table, oid, owner=owner, include_expired=include_expired,
                    as_of=as_of, rel=rel_filter, min_weight=min_weight,
                    session_id=session_id, limit=limit):
                ntable, noid = neighbor["table"], int(neighbor["id"])
                if not self.node_ok(ntable, noid, owner=owner,
                                    include_expired=include_expired,
                                    as_of=as_of, session_id=session_id):
                    continue
                if not add_link(neighbor, depth + 1):
                    break
                enqueue(ntable, noid, depth + 1)

        edges_out = sorted(edges.values(), key=lambda row: (row["depth"], row["id"]))
        links_out = sorted(links.values(), key=lambda row: (row["depth"], row["id"]))
        return {"edges": edges_out, "links": links_out,
                "truncated": truncated or total >= limit}

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
    def _duplicate_facts(self, threshold: float = 0.95, cap: int = 20,
                         per_owner: int = 500) -> list:
        """A5: near-дубли ЖИВЫХ фактов ВНУТРИ owner по косинусу (read-only).

        Порог 0.95 фиксирован (косвенный сигнал, не приговор), пары капнуты.
        Без векторов → пусто. owner-изоляция соблюдается группировкой.
        """
        vecs = self.conn.execute(
            "SELECT owner_id, owner, embedding FROM um_vectors"
            " WHERE owner_table='um_facts'").fetchall()
        if not vecs:
            return []
        live = {r[0] for r in self.conn.execute(
            "SELECT id FROM um_facts WHERE valid_until=0")}
        by_owner: dict = {}
        for oid, owner, blob in vecs:
            if oid not in live or not blob:
                continue
            by_owner.setdefault(owner or "", []).append((oid, unpack_vector(blob)))
        pairs: list = []
        for items in by_owner.values():
            items = items[:per_owner]
            for i in range(len(items)):
                vi = items[i][1]
                for j in range(i + 1, len(items)):
                    vj = items[j][1]
                    if len(vi) != len(vj) or not vi:
                        continue
                    s = cosine(vi, vj)
                    if s >= threshold:
                        pairs.append(["um_facts", items[i][0], items[j][0],
                                      round(s, 3)])
                        if len(pairs) >= cap:
                            return pairs
        return pairs

    def _dangling_conditions(self) -> tuple[str, list]:
        """SQL-условие «живой линк с удалённым концом» (имена таблиц — из LINK_TABLES)."""
        conds: list = []
        params: list = []
        for t in LINK_TABLES:
            conds.append(f"(src_table=? AND src_id NOT IN (SELECT id FROM {t}))")
            params.append(t)
            conds.append(f"(dst_table=? AND dst_id NOT IN (SELECT id FROM {t}))")
            params.append(t)
        return " OR ".join(conds), params

    def _dangling_link_ids(self, limit: int = 0) -> list[int]:
        where, params = self._dangling_conditions()
        q = f"SELECT id FROM um_links WHERE valid_until=0 AND ({where})"
        if limit:
            q += " LIMIT ?"
            params = [*params, limit]
        return [r[0] for r in self.conn.execute(q, params).fetchall()]

    def hygiene(self) -> dict:
        """Кандидаты мусора без мутаций: сироты векторов/FTS/сущностей, висячий vec0."""
        out: dict = {"orphan_vectors": [], "orphan_fts": [],
                     "orphan_entities": 0, "dangling_vecidx": [],
                     "dangling_links": [], "duplicate_facts": []}
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
        out["dangling_links"] = self._dangling_link_ids(limit=11)
        out["duplicate_facts"] = self._duplicate_facts()
        return out

    def rebuild_fts(self) -> bool:
        """Полная пересборка um_fts из родителей (формат тел — как _fts_index).

        Без лока: вызывается из locked-контекста (repair) и из import-транзакции.
        """
        if not self.fts:
            return False
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
        return True

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
            self.rebuild_fts()
            report["fts_rebuilt"] = True
        self._prune_orphan_entities()
        dwhere, dparams = self._dangling_conditions()
        cur = self.conn.execute(
            f"DELETE FROM um_links WHERE valid_until=0 AND ({dwhere})", dparams)
        report["purged_links"] = cur.rowcount
        if dim > 0:
            try:
                report["vec_index"] = self.build_vec_index(dim)
            except Exception as e:
                report["vec_index_error"] = f"{type(e).__name__}: {e}"[:200]
        self.conn.commit()
        self._restrict_artifacts()
        return report

    @_locked
    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self._restrict_artifacts()


def open_db(cfg: Config, embedding_dim: int, embedding_model: str):
    """Совместимость со stage-0 API: возвращает Store."""
    return Store(cfg, embedding_dim, embedding_model)
