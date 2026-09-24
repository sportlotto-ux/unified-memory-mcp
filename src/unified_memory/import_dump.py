"""Импорт дампа (v0.8 B6). CLI: python -m unified_memory.import_dump <file>
[--owner X] [--dry-run]. БД — из UM_DATABASE_PATH (как у сервера).

Контракт:
- Fresh-id remap ВСЕГДА: строки вставляются без id, ссылки маппятся old→new.
  Коллизии исключены конструкцией (новые id автоинкрементны).
- Аддитивность: ни одного UPDATE существующих строк. Слот-конфликт
  (owner, category, name) → skip + счётчик. um_meta не вставляется (из дампа
  читается только schema_version).
- Вектора — из дампа, пересчёта нет (импорт работает без backend). Чужая dim →
  skip + dim_mismatch в отчёте (иначе recall.len-фильтр молча не находит).
  После транзакции — build_vec_index() (коммитит внутри, в контур нельзя):
  knn-плечо видит импортированные вектора сразу, backend не нужен.
- Без redaction-гейта: контент уже пост-redaction, повторный _clean покалечил бы
  плейсхолдеры.
- Валидация: неизвестная/новейшая schema_version → отказ; неизвестная таблица →
  отказ (не silent-skip). CHECK-и SQLite — второй рубеж.
- Весь импорт в transaction() (ядро 5a); dry_run — тем же контуром.
- История/время — данные: valid_until/superseded_by/created_at как есть.
- Чтение (read_dump) собирает {table: [rows]} в память целиком — стриминг только
  на записи (D16). Для v1 честное ограничение: гигабайтный дамп при загрузке
  держится в RAM.
- Угловой случай skip-преемника: если новая версия пропущена (слот занят),
  superseded_by вставленной старой версии → 0 (ссылка на пропущенного). Разрыв
  цепочки видимый, не ложный — поведение, не баг.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from .config import load
from .export import COMPLETE_FORMAT
from .store import Store, vec_extension_available

FORMAT = "um-export-jsonl"
SUPPORTED = ("1",)

# Вставляемые таблицы в порядке зависимостей.
IMPORT_TABLES = ("um_entities", "um_facts", "um_messages", "um_summaries",
                 "um_summary_sources", "um_edges", "um_links", "um_vectors")
# um_meta читаем (schema_version), um_fts/um_vecidx — производные (legacy-дампы
# их содержат), при импорте игнорируются: FTS пересобирается, vecidx — reindex.
IGNORED = ("um_meta", "um_fts", "um_vecidx")
KNOWN = set(IMPORT_TABLES) | set(IGNORED)


def _decode_emb(emb) -> bytes:
    """embedding дампа → bytes. Битый base64/тип — b'' (не исключение: импорт
    аддитивный, одна битая строка не должна ронять весь дамп)."""
    try:
        if isinstance(emb, str):
            return base64.b64decode(emb)
        return bytes(emb)
    except Exception:
        return b""


def _blob_dim(blob: bytes) -> int:
    """dim float32-blobа (0 = не вектор)."""
    if blob and len(blob) % 4 == 0:
        return len(blob) // 4
    return 0


def _target_vec_dim(store, dump_rows) -> int:
    """Эталон dim для импорта: активный индекс → первый вектор стора → первый
    валидный вектор дампа. 0 = векторов нигде нет (валидировать не с чем)."""
    raw = store.meta_get("vec_index_dim")
    if raw and raw.isdigit() and int(raw) > 0:
        return int(raw)
    ex = store.conn.execute("SELECT embedding FROM um_vectors LIMIT 1").fetchone()
    if ex:
        d = _blob_dim(bytes(ex[0]))
        if d:
            return d
    for r in dump_rows:
        d = _blob_dim(_decode_emb(r.get("embedding")))
        if d:
            return d
    return 0


def read_dump(path: str | Path) -> tuple[str, dict[str, list[dict]]]:
    """(schema_version, {table: [row]}). Понимает JSONL и legacy single-JSON."""
    raw = Path(path).read_text(encoding="utf-8")
    lines = raw.splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    try:
        head = json.loads(first)
    except json.JSONDecodeError:
        raise ValueError("dump is neither JSONL nor JSON: bad first line")
    if isinstance(head, dict) and head.get("format") == FORMAT:
        body_lines = [ln for ln in lines[1:] if ln.strip()]
        complete = False
        if body_lines:
            try:
                last = json.loads(body_lines[-1])
            except json.JSONDecodeError as e:
                if head.get("complete") is True:
                    raise ValueError("incomplete dump: invalid completion marker") from e
            else:
                if isinstance(last, dict) and last.get("format") == COMPLETE_FORMAT:
                    complete = True
                    body_lines.pop()
        if head.get("complete") is True and not complete:
            raise ValueError("incomplete dump: missing completion marker")
        tables: dict[str, list[dict]] = {}
        for ln in body_lines:
            obj = json.loads(ln)
            t = obj.get("table")
            if t not in KNOWN:
                raise ValueError(f"unknown table {t!r} in dump")
            tables.setdefault(t, []).append(obj["row"])
        if head.get("complete") is True:
            counts = head.get("counts") or {}
            actual = {table: len(rows) for table, rows in tables.items()}
            for table, expected in counts.items():
                if actual.get(table, 0) != int(expected):
                    raise ValueError(
                        f"incomplete dump: count mismatch for {table} "
                        f"(expected {expected}, got {actual.get(table, 0)})")
        return str(head.get("schema_version") or ""), tables
    if isinstance(head, dict) and "tables" in head:  # legacy: весь файл одним JSON
        payload = json.loads(raw)
        for t in payload["tables"]:
            if t not in KNOWN:
                raise ValueError(f"unknown table {t!r} in dump")
        return str(payload.get("schema_version") or ""), payload["tables"]
    raise ValueError("unrecognized dump format")


def _own(owner, row):
    return owner if owner is not None else (row.get("owner") or "")


def _entities(store, rows, owner, rep):
    m: dict[int, int] = {}
    for r in rows:
        own = _own(owner, r)
        ex = store.conn.execute(
            "SELECT id FROM um_entities WHERE name=? AND owner=?",
            (r["name"], own)).fetchone()
        if ex:  # имя уже есть: переиспользуем (аддитивно), не вставляем
            m[r["id"]] = ex[0]
            rep["um_entities"]["skipped"] += 1
            continue
        cur = store.conn.execute(
            "INSERT INTO um_entities(name, display, created_at, owner)"
            " VALUES(?,?,?,?)",
            (r["name"], r.get("display") or r["name"],
             float(r.get("created_at") or 0.0), own))
        m[r["id"]] = cur.lastrowid
        rep["um_entities"]["inserted"] += 1
    return m


def _facts(store, rows, owner, rep):
    m: dict[int, int] = {}
    for r in rows:
        own = _own(owner, r)
        vu = float(r.get("valid_until") or 0.0)
        if vu == 0:  # слот занят живой версией → skip (lossless-safe)
            ex = store.conn.execute(
                "SELECT id FROM um_facts WHERE owner=? AND category=? AND name=?"
                " AND valid_until=0", (own, r["category"], r["name"])).fetchone()
            if ex:
                rep["um_facts"]["skipped"] += 1
                continue
        cur = store.conn.execute(
            "INSERT INTO um_facts(owner, category, name, body, importance,"
            " created_at, updated_at, valid_until, superseded_by)"
            " VALUES(?,?,?,?,?,?,?,?,0)",
            (own, r["category"], r["name"], r.get("body", ""),
             float(r.get("importance") or 0.5),
             float(r.get("created_at") or 0.0),
             float(r.get("updated_at") or 0.0), vu))
        m[r["id"]] = cur.lastrowid
        rep["um_facts"]["inserted"] += 1
    # второй проход: superseded_by ссылается на факт, который мог вставиться позже
    # (UPDATE только по НОВЫМ строкам — существующие не трогаем)
    for r in rows:
        nid = m.get(r["id"])
        if nid is not None and int(r.get("superseded_by") or 0):
            store.conn.execute("UPDATE um_facts SET superseded_by=? WHERE id=?",
                               (m.get(r["superseded_by"], 0), nid))
    return m


def _messages(store, rows, owner, rep):
    m: dict[int, int] = {}
    for r in rows:
        cur = store.conn.execute(
            "INSERT INTO um_messages(session_id, owner, role, content, created_at,"
            " source, externalized_ref) VALUES(?,?,?,?,?,?,?)",
            (r["session_id"], _own(owner, r), r["role"], r["content"],
             float(r.get("created_at") or 0.0), r.get("source") or "unknown",
             r.get("externalized_ref")))
        m[r["id"]] = cur.lastrowid
        rep["um_messages"]["inserted"] += 1
    return m


def _summaries(store, rows, mmap, owner, rep):
    m: dict[int, int] = {}
    for r in rows:
        cf, ct = r.get("covers_from"), r.get("covers_to")
        cur = store.conn.execute(
            "INSERT INTO um_summaries(session_id, owner, depth, body, covers_from,"
            " covers_to, superseded_by, created_at) VALUES(?,?,?,?,?,?,0,?)",
            (r["session_id"], _own(owner, r), int(r.get("depth") or 0),
             r["body"], mmap.get(cf, cf), mmap.get(ct, ct),
             float(r.get("created_at") or 0.0)))
        m[r["id"]] = cur.lastrowid
        rep["um_summaries"]["inserted"] += 1
    for r in rows:  # superseded_by — ссылка на саммари (второй проход)
        nid = m.get(r["id"])
        if nid is not None and int(r.get("superseded_by") or 0):
            store.conn.execute(
                "UPDATE um_summaries SET superseded_by=? WHERE id=?",
                (m.get(r["superseded_by"], 0), nid))
    return m


def _summary_sources(store, rows, mmap, smap, owner, rep):
    for r in rows:
        sid = smap.get(int(r.get("summary_id") or 0))
        source_table = r.get("source_table")
        source_id = (mmap if source_table == "um_messages" else smap).get(
            int(r.get("source_id") or 0))
        if not sid or source_table not in ("um_messages", "um_summaries")                 or not source_id:
            rep["um_summary_sources"]["skipped"] += 1
            continue
        store.conn.execute(
            "INSERT OR IGNORE INTO um_summary_sources"
            "(summary_id, source_table, source_id, position) VALUES(?,?,?,?)",
            (sid, source_table, source_id, int(r.get("position") or 0)))
        rep["um_summary_sources"]["inserted"] += 1


def _edges(store, rows, emap, fmap, owner, rep):
    m: dict[int, int] = {}
    for r in rows:
        sid, oid = emap.get(r["subject_id"]), emap.get(r["object_id"])
        if sid is None or oid is None:  # конец не вставлен → skip
            rep["um_edges"]["skipped"] += 1
            continue
        cur = store.conn.execute(
            "INSERT INTO um_edges(subject_id, predicate, object_id, session_id,"
            " owner, fact_id, created_at, valid_until) VALUES(?,?,?,?,?,?,?,?)",
            (sid, r["predicate"], oid, r.get("session_id") or "",
             _own(owner, r), fmap.get(r.get("fact_id"), 0),
             float(r.get("created_at") or 0.0), float(r.get("valid_until") or 0.0)))
        m[r["id"]] = cur.lastrowid
        rep["um_edges"]["inserted"] += 1
    return m


def _links(store, rows, maps, owner, rep):
    for r in rows:
        own = _own(owner, r)
        st, dt = r["src_table"], r["dst_table"]
        sid = maps.get(st, {}).get(r["src_id"])
        did = maps.get(dt, {}).get(r["dst_id"])
        if sid is None or did is None:  # конец не вставлен → skip
            rep["um_links"]["skipped"] += 1
            continue
        vu = float(r.get("valid_until") or 0.0)
        if vu == 0:  # живая связь (src,dst,rel,owner) уникальна
            ex = store.conn.execute(
                "SELECT id FROM um_links WHERE src_table=? AND src_id=? AND"
                " dst_table=? AND dst_id=? AND rel=? AND owner=? AND valid_until=0",
                (st, sid, dt, did, r["rel"], own)).fetchone()
            if ex:
                rep["um_links"]["skipped"] += 1
                continue
        store.conn.execute(
            "INSERT INTO um_links(src_table, src_id, dst_table, dst_id, rel, weight,"
            " owner, session_id, created_at, valid_until) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (st, sid, dt, did, r["rel"], float(r.get("weight", 1.0)), own,
             r.get("session_id") or "", float(r.get("created_at") or 0.0), vu))
        rep["um_links"]["inserted"] += 1


def _vectors(store, rows, maps, owner, rep, target_dim):
    for r in rows:
        ot = r["owner_table"]
        nid = maps.get(ot, {}).get(r["owner_id"])
        if nid is None:  # контент не вставлен → вектор не нужен
            rep["um_vectors"]["skipped"] += 1
            continue
        blob = _decode_emb(r.get("embedding"))
        d = _blob_dim(blob)
        if not d or (target_dim and d != target_dim):
            # Чужая размерность (или битый blob): recall.len-фильтр её бы молча
            # не нашёл — считаем вслух, в отчёт, не вставляем.
            rep["um_vectors"]["dim_mismatch"] += 1
            continue
        store.conn.execute(
            "INSERT INTO um_vectors(owner_table, owner_id, embedding, model, owner)"
            " VALUES(?,?,?,?,?)",
            (ot, nid, blob, r["model"], _own(owner, r)))
        rep["um_vectors"]["inserted"] += 1


def import_dump(store: Store, path: str | Path, owner: str | None = None,
                dry_run: bool = False) -> dict:
    sv, tables = read_dump(path)
    if sv not in SUPPORTED:
        raise ValueError(
            f"unsupported dump schema_version {sv!r}; supported {SUPPORTED}")
    rep = {t: {"inserted": 0, "skipped": 0} for t in IMPORT_TABLES}
    rep["um_vectors"]["dim_mismatch"] = 0
    vec_rows = tables.get("um_vectors", [])
    target_dim = _target_vec_dim(store, vec_rows)
    with store.transaction(dry_run=dry_run):
        emap = _entities(store, tables.get("um_entities", []), owner, rep)
        fmap = _facts(store, tables.get("um_facts", []), owner, rep)
        mmap = _messages(store, tables.get("um_messages", []), owner, rep)
        smap = _summaries(store, tables.get("um_summaries", []), mmap, owner, rep)
        _summary_sources(store, tables.get("um_summary_sources", []),
                         mmap, smap, owner, rep)
        edgemap = _edges(store, tables.get("um_edges", []), emap, fmap, owner, rep)
        maps = {"um_entities": emap, "um_facts": fmap, "um_messages": mmap,
                "um_summaries": smap, "um_edges": edgemap}
        _links(store, tables.get("um_links", []), maps, owner, rep)
        _vectors(store, vec_rows, maps, owner, rep, target_dim)
        # Imported rows bypass Store write helpers; invalidate all derived
        # pressure state so the next read rebuilds it from durable rows.
        store.conn.execute(
            "DELETE FROM um_meta WHERE key LIKE 'tokens:%'"
            " OR key LIKE 'raw_tokens:%' OR key LIKE 'summary_tokens:%'"
            " OR key LIKE 'frontier:%'")
        store.rebuild_fts()
    # build_vec_index коммитит внутри — только ПОСЛЕ транзакции (guardrail: commit
    # внутри разорвал бы контур). Backend не нужен: источник — um_vectors,
    # требуется лишь extra local-vec. Без вставленных векторов индекс не трогаем.
    if not dry_run and rep["um_vectors"]["inserted"]:
        if vec_extension_available():
            try:
                rep["vec_index"] = store.build_vec_index(target_dim)
            except Exception as e:
                rep["vec_index_error"] = f"{type(e).__name__}: {e}"[:200]
        else:
            rep["vec_index"] = "skipped_no_local_vec"
    rep["schema_version"] = sv
    rep["dry_run"] = dry_run
    rep["totals"] = {
        "inserted": sum(v["inserted"] for v in rep.values() if isinstance(v, dict)),
        "skipped": sum(v["skipped"] for v in rep.values() if isinstance(v, dict))}
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m unified_memory.import_dump")
    ap.add_argument("file", help="dump file (JSONL, or legacy single-JSON)")
    ap.add_argument("--owner", default=None, help="override tenant of all rows")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate + report, change nothing")
    args = ap.parse_args(argv)
    store = Store(load())
    try:
        rep = import_dump(store, args.file, owner=args.owner, dry_run=args.dry_run)
    finally:
        store.close()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
