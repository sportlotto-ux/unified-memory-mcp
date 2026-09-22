"""Unified store (этап 1 — пока DDL-план + open-хелпер, данные не мигрируем).

Одна SQLite вместо двух:
- mnemosyne.db (~30 таблиц: canonical_facts, working_memory, episodic_memory,
  triples, memoria_*, memory_embeddings, scratchpad, sync_*)
- lcm.db (~30 таблиц lcm_*: messages, summary_nodes, rollups, trajectory_*,
  query_views, assertion_*, chunk_*/embedding_*)

Схема слияния: неймспейс-префикс ``um_`` + сохранение исходных имён
как VIEW для обратной совместимости на переходный период.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import Config
from .embeddings import DimensionMismatchError, check_store_dim  # noqa: F401 (контракт слоя)

SCHEMA = """
PRAGMA journal_mode=WAL;

-- Сырые сообщения (источник: lcm.messages + mnemosyne episodic/working ingest).
CREATE TABLE IF NOT EXISTS um_messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'unknown',  -- lineage вместо двух разных полей
    externalized_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_um_messages_session ON um_messages(session_id, id);

-- DAG саммари (источник: lcm.summary_nodes + lcm_rollups).
CREATE TABLE IF NOT EXISTS um_summaries (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,
    covers_from INTEGER,  -- fk um_messages.id
    covers_to INTEGER,
    created_at REAL NOT NULL
);

-- Долгие факты (источник: canonical_facts + memoria_* + triples).
CREATE TABLE IF NOT EXISTS um_facts (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,   -- canonical-слоты: identity/preference/credential/skill-refs...
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,  -- капаем сверху: жёстких 0.95 как в mnemosyne нет
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Вектора (источник: memory_embeddings + lcm_chunk_vectors/lcm_embedding_*).
-- dim контролируется embeddings.check_store_dim при open().
CREATE TABLE IF NOT EXISTS um_vectors (
    id INTEGER PRIMARY KEY,
    owner_table TEXT NOT NULL,  -- 'um_messages' | 'um_summaries' | 'um_facts'
    owner_id INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    model TEXT NOT NULL
);

-- Мета стора (модель эмбеддингов, версия схемы).
CREATE TABLE IF NOT EXISTS um_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def open_db(cfg: Config, embedding_dim: int, embedding_model: str) -> sqlite3.Connection:
    """Открыть/создать unified store. Падает LOUD при расхождении dim."""
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cfg.db_path))
    conn.executescript(SCHEMA)

    def meta_getter(key: str) -> str | None:
        row = conn.execute("SELECT value FROM um_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    check_store_dim(embedding_dim, embedding_model, meta_getter)
    if meta_getter("embedding_model") is None:
        conn.executemany(
            "INSERT OR IGNORE INTO um_meta(key, value) VALUES(?, ?)",
            [("embedding_model", embedding_model),
             ("embedding_dim", str(embedding_dim)),
             ("schema_version", "1")],
        )
        conn.commit()
    return conn
