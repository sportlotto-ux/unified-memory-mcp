"""Холодный архив (v0.5-п.3, вариант a2).

Отдельный SQLite-файл. При срабатывании порога старейшие сообщения уезжают
туда целиком (текст + вектор); в горячей БД остаётся строка-заглушка
`[archived]` с `externalized_ref = "<archive>#<id>"`, по которой `mem_expand`
прозрачно достаёт текст. Ретеншн-удаление работает только по архиву и только
по сроку: «не сохранив — не удаляем».
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ar_messages(
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    owner TEXT,
    role TEXT,
    content TEXT,
    created_at REAL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS ar_msg_created ON ar_messages(created_at);
CREATE TABLE IF NOT EXISTS ar_vectors(
    id INTEGER PRIMARY KEY,
    owner_table TEXT,
    owner_id INTEGER,
    embedding BLOB,
    model TEXT,
    owner TEXT
);
CREATE INDEX IF NOT EXISTS ar_vec_owner ON ar_vectors(owner_table, owner_id);
"""


def open_archive(path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def move_oldest(store, conn: sqlite3.Connection, limit: int = 500,
                before_ts: float = 0.0, label: str = "") -> int:
    """Старейшие сообщения -> архив (текст+вектор), в горячей — заглушка (a2)."""
    rows = store.oldest_messages(limit, before_ts)
    n = 0
    for m in rows:
        conn.execute(
            "INSERT OR REPLACE INTO ar_messages(id, session_id, owner, role,"
            " content, created_at, source) VALUES(?,?,?,?,?,?,?)",
            (m["id"], m["session_id"], m["owner"], m["role"], m["content"],
             m["created_at"], m["source"]))
        vec = store.message_vector(m["id"])
        if vec is not None:
            conn.execute(
                "INSERT OR REPLACE INTO ar_vectors(owner_table, owner_id,"
                " embedding, model, owner) VALUES('um_messages',?,?,?,?)",
                (m["id"], vec[0], vec[1], m["owner"]))
        store.mark_archived(m["id"], f"{label}#{m['id']}")
        n += 1
    conn.commit()
    return n


def purge_older_than(conn: sqlite3.Connection, ts: float) -> int:
    """Физическое удаление из АРХИВА старше ts. Возвращает число сообщений."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM ar_messages WHERE created_at < ?", (ts,))]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM ar_messages WHERE id IN ({ph})", ids)
    conn.execute(
        f"DELETE FROM ar_vectors WHERE owner_table='um_messages'"
        f" AND owner_id IN ({ph})", ids)
    conn.commit()
    return len(ids)


def fetch_message(conn: sqlite3.Connection, mid: int) -> dict | None:
    r = conn.execute(
        "SELECT session_id, owner, role, content, created_at, source"
        " FROM ar_messages WHERE id=?", (mid,)).fetchone()
    if not r:
        return None
    return dict(zip(["session_id", "owner", "role", "content",
                     "created_at", "source"], r))


def search(conn: sqlite3.Connection, terms: list[str], limit: int = 20) -> list[dict]:
    """LIKE-поиск по архиву (для include_archived)."""
    terms = [t for t in terms if t]
    if not terms:
        return []
    cond = " OR ".join(["content LIKE ?"] * len(terms))
    params = [f"%{t}%" for t in terms]
    rows = conn.execute(
        f"SELECT id, session_id, content, created_at FROM ar_messages"
        f" WHERE {cond} ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall()
    return [{"id": r[0], "session_id": r[1], "content": r[2], "created_at": r[3]}
            for r in rows]


def status(conn: sqlite3.Connection, path) -> dict:
    p = Path(path)
    return {
        "archive_path": str(p),
        "archive_bytes": p.stat().st_size if p.exists() else 0,
        "archived_messages": conn.execute(
            "SELECT count(*) FROM ar_messages").fetchone()[0],
        "archived_vectors": conn.execute(
            "SELECT count(*) FROM ar_vectors").fetchone()[0],
    }
