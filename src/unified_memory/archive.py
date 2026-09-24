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
from urllib.parse import quote

from .file_permissions import ensure_private_parent, restrict_new_sqlite_files
from .store import tokenize

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
    seen: set[Path] = {
        Path(f"{p}{suffix}")
        for suffix in ("", "-wal", "-shm")
        if Path(f"{p}{suffix}").exists()
    }
    ensure_private_parent(p)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    restrict_new_sqlite_files(p, seen)
    return conn


def open_archive_readonly(path) -> sqlite3.Connection:
    """Open an existing archive without creating or migrating the file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    uri = f"file:{quote(str(p), safe='/:')}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def audit(store, path) -> dict:
    """B7: read-only сверка горячих заглушек с архивом. Архив НЕ создаётся.

    externalized_ref = "<label>#<archive_id>"; orphan — заглушка, чей id в
    архиве не найден (архив затёрт/перенесён) либо ref не парсится.
    """
    out = {"file_exists": bool(path) and Path(path).exists(),
           "archive_path": str(path), "stubs": 0, "archived_rows": 0, "orphans": 0}
    stubs = store.select(
        "SELECT externalized_ref FROM um_messages"
        " WHERE externalized_ref IS NOT NULL AND externalized_ref!=''")
    out["stubs"] = len(stubs)
    arch_ids: set = set()
    if out["file_exists"]:
        # file:-URI: путь экранируем (?/# в имени иначе ломают mode=ro).
        # safe="/:" — слэши и буква диска остаются как есть.
        uri = f"file:{quote(str(path), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            try:
                arch_ids = {r[0] for r in conn.execute("SELECT id FROM ar_messages")}
            except sqlite3.OperationalError:  # пустой/чужой файл
                arch_ids = set()
        finally:
            conn.close()
    out["archived_rows"] = len(arch_ids)
    out["orphans"] = sum(1 for r in stubs
                         if _ref_id(r[0]) not in arch_ids)
    return out


def _ref_id(ref) -> int | None:
    try:
        return int(str(ref).rsplit("#", 1)[-1])
    except (ValueError, IndexError):
        return None


def move_oldest(store, conn: sqlite3.Connection, limit: int = 500,
                before_ts: float = 0.0, label: str = "") -> int:
    """Старейшие сообщения -> архив (текст+вектор), в горячей — заглушка (a2).

    Cold SQLite commit выполняется до первого hot commit. Между двумя
    независимыми файлами нет 2PC: обратный порядок оставлял бы hot stub без
    архивной строки при crash. Повторный проход безопасен благодаря
    INSERT OR REPLACE и уже durable cold rows.
    """
    rows = store.oldest_messages(limit, before_ts)
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
    # Сначала делаем cold-копию долговечной; только затем меняем hot DB.
    conn.commit()
    for m in rows:
        store.mark_archived(m["id"], f"{label}#{m['id']}")
    return len(rows)


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


def search_messages(conn: sqlite3.Connection, query: str, scope: str = "all",
                    session_id: str = "", limit: int = 20, owner: str = "",
                    source: str = "", max_scan: int = 2000,
                    label: str = "") -> list[dict]:
    """Bounded cold-message search; scope filters run before the scan cap."""
    if scope == "facts":
        return []
    terms = tokenize(query)
    if not terms or limit <= 0 or max_scan <= 0:
        return []
    conditions = []
    params: list = []
    if scope == "session":
        if not session_id:
            return []
        conditions.append("session_id=?")
        params.append(session_id)
    if owner:
        conditions.append("owner=?")
        params.append(owner)
    if source:
        conditions.append("source=?")
        params.append(source)
    where = " AND ".join(conditions) or "1"
    term_where = " OR ".join(["content LIKE ?"] * min(6, len(terms)))
    term_params = [f"%{term}%" for term in terms[:6]]
    sql = (
        "SELECT id, session_id, owner, role, content, created_at, source"
        " FROM (SELECT id, session_id, owner, role, content, created_at, source"
        f"       FROM ar_messages WHERE {where}"
        "       ORDER BY created_at DESC, id DESC LIMIT ?)"
        f" WHERE {term_where} ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    rows = conn.execute(sql, (*params, max_scan, *term_params, limit)).fetchall()
    keys = ["id", "session_id", "owner", "role", "content", "created_at", "source"]
    out = []
    for row in rows:
        item = dict(zip(keys, row))
        if label:
            item["archive_ref"] = f"{label}#{item['id']}"
        out.append(item)
    return out


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
