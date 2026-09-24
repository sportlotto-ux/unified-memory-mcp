"""Экспорт стора в JSONL-файл (v0.8 D16, read-only, стриминговый).

Формат (строка = один JSON-объект, \n-разделённые):
  1-я строка — header: {"format":"um-export-jsonl", "schema_version", "exported_at",
                        "source_db", "counts", "complete": true}
  далее      — {"table": <имя>, "row": {...}} по объекту на строку.
  последняя  — {"format":"um-export-jsonl-complete"}.

Что внутри: контент + um_links, вектора base64 (lossless). um_fts — исключён
(производный; перестраивается _fts_index при импорте, иначе пришлось бы ремаппить
его id). um_vecidx — исключён (пересборка через reindex). um_meta — дампится,
но импортёр читает из него только schema_version. Архив — вне дампа (отдельный
файл, вечный холод). Пишем курсором/чанками, не собирая дамп в память.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from pathlib import Path

from .store import Store

FORMAT = "um-export-jsonl"
COMPLETE_FORMAT = "um-export-jsonl-complete"

# Контент-таблицы в стабильном порядке. um_fts/um_vecidx — производные
# (см. docstring), в дамп не входят.
CONTENT_TABLES = ("um_messages", "um_summaries", "um_summary_sources",
                   "um_facts", "um_entities", "um_edges", "um_links",
                   "um_vectors", "um_meta")


def _encode_row(cols: list[str], row: tuple) -> dict:
    d = dict(zip(cols, row))
    for k, v in d.items():
        if isinstance(v, (bytes, bytearray, memoryview)):
            d[k] = base64.b64encode(bytes(v)).decode("ascii")
    return d


def _reject_database_output(store: Store, out_path: Path) -> None:
    db_path = Path(store._db_path).expanduser().resolve()
    protected = {db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")}
    resolved = out_path.expanduser().resolve()
    if resolved in protected:
        raise ValueError("export output must not be the database, WAL, or SHM file")


def export_store(store: Store, path: str | Path | None = None) -> dict:
    """JSONL-дамп. Имя по умолчанию: <db>.export-<ts>.jsonl.

    Весь проход — под store.read_locked(): консистентный снепшот без гонки с
    писателями (FastMCP-треды делят один conn). Файл сначала пишется во
    временный файл с mode 0600, затем публикуется через os.replace().
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = Path(path) if path else Path(f"{store._db_path}.export-{ts}.jsonl")
    _reject_database_output(store, out_path)
    tmp_path: Path | None = None
    with store.read_locked():
        counts = {t: store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in CONTENT_TABLES}
        header = {"format": FORMAT,
                  "schema_version": store.meta_get("schema_version") or "1",
                  "exported_at": time.time(), "source_db": store._db_path,
                  "counts": counts, "complete": True}
        written = 0
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=out_path.parent,
                    prefix=f".{out_path.name}.", suffix=".tmp", delete=False) as f:
                tmp_path = Path(f.name)
                line = json.dumps(header, ensure_ascii=False) + "\n"
                f.write(line)
                written += len(line.encode("utf-8"))
                for t in CONTENT_TABLES:
                    cols = [r[1] for r in store.conn.execute(f"PRAGMA table_info({t})")]
                    for row in store.conn.execute(f"SELECT * FROM {t}"):
                        line = json.dumps({"table": t, "row": _encode_row(cols, row)},
                                          ensure_ascii=False) + "\n"
                        f.write(line)
                        written += len(line.encode("utf-8"))
                footer = json.dumps({"format": COMPLETE_FORMAT}, ensure_ascii=False) + "\n"
                f.write(footer)
                written += len(footer.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, out_path)
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
    return {"path": str(out_path), "bytes": written, "counts": counts,
            "schema_version": header["schema_version"], "format": FORMAT,
            "archive_included": False, "streaming": True}
